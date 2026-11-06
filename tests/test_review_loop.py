"""Review loop with fake model clients: validator rejections (out-of-allowlist ids etc.), all three critic
verdicts and their gate behaviour, triage stop, replay dry-run, and golden-file shape checks."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from argusvue.agents import theses as T
from argusvue.agents.packet import build_packet
from argusvue.agents.review import due_theses, review_thesis
from argusvue.agents.schemas import AnalystProposal, CriticReview, EntailmentVerdict, TriageVerdict
from argusvue.agents.validator import validate
from argusvue.store.blobs import BlobStore

pytestmark = pytest.mark.db
GOLDEN = Path(__file__).parent / "golden"


def _resp(payload: dict | None = None, *, model_cls=None, text: str | None = None):
    parsed = model_cls.model_validate(payload) if (payload is not None and model_cls) else None
    return SimpleNamespace(parsed_output=parsed, usage=SimpleNamespace(input_tokens=3000, output_tokens=600, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0, server_tool_use=None),
                           content=[SimpleNamespace(type="text", text=text or json.dumps(payload), citations=None)],
                           stop_reason="end_turn", stop_details=None)


class ScriptedClient:
    """Returns responses keyed by role (the system prompt's first line tells the role)."""

    def __init__(self):
        self.by_role: dict[str, list] = {}
        self.calls: list[tuple[str, dict]] = []
        self.messages = self

    def role_of(self, kw) -> str:
        sys_text = kw["system"][0]["text"]
        if sys_text.startswith("You triage"):
            return "triage"
        if sys_text.startswith("You are the analyst"):
            return "analyst"
        if sys_text.startswith("You are the critic"):
            return "critic"
        if sys_text.startswith("Decide whether the cited"):
            return "entailment"
        return "other"

    def parse(self, **kw):
        role = self.role_of(kw)
        self.calls.append((role, kw))
        q = self.by_role.get(role) or []
        if not q:
            raise AssertionError(f"no scripted response for role {role}")
        return q.pop(0)

    create = parse


@pytest.fixture
def world(synthetic):
    """Region tst with an indicator series, two web documents from distinct origins, three extracted claims,
    and one seeded thesis."""
    conn = synthetic
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO sources(slug, name, kind, region_id, reliability, origin_key) VALUES
                       ('s-struct','S','structured','tst',0.9,'struct'), ('news-a','A','feed','tst',0.7,'news-a.test'),
                       ('news-b','B','feed','tst',0.6,'news-b.test') RETURNING id, slug""")
        ids = {r["slug"]: r["id"] for r in cur.fetchall()}
        cur.execute("""INSERT INTO indicators(id, name, unit, direction_good, half_life_days, source_id, region_scope, frequency)
                       VALUES ('zhvi','Z','USD','neutral',45,%s,'{tst}','monthly')""", (ids["s-struct"],))
        cur.execute("""INSERT INTO documents(source_id, url, fetched_at, content_hash, blob_ref, origin_key, title) VALUES
                       (%s,'https://s/1',now(),decode('01','hex'),'b','struct','z'),
                       (%s,'https://news-a.test/1',now(),decode('02','hex'),'b','news-a.test','A1'),
                       (%s,'https://news-b.test/1',now(),decode('03','hex'),'b','news-b.test','B1') RETURNING id""",
                    (ids["s-struct"], ids["news-a"], ids["news-b"]))
        d_s, d_a, d_b = [r["id"] for r in cur.fetchall()]
        cur.execute("INSERT INTO entities(type, canonical_name, region_id, geo_key, status) VALUES ('place','ZCTA 99001','tst','zcta:99001','canonical'), ('project','Deer Valley Apts','tst','zcta:99001','candidate') RETURNING id")
        e_geo, e_proj = [r["id"] for r in cur.fetchall()]
        base = date(2023, 9, 1)
        for i in range(37):
            p = date(base.year + (base.month - 1 + i) // 12, (base.month - 1 + i) % 12 + 1, 1)
            cur.execute("""INSERT INTO claims(subject_entity, predicate, value_num, geo_key, valid_from, observed_at, document_id, excerpt,
                           extractor_version, source_reliability, extraction_conf, confidence) VALUES (%s,'indicator:zhvi',%s,'zcta:99001',%s,%s,%s,'row','structured@1',0.9,1,0.9) RETURNING id""",
                        (e_geo, 480000 + 1000 * i, p, datetime.now(UTC) - timedelta(days=5), d_s))
            cid = cur.fetchone()["id"]
            cur.execute("INSERT INTO indicator_points(indicator_id, geo_key, period, value, claim_id, observed_at) VALUES ('zhvi','zcta:99001',%s,%s,%s,%s)",
                        (p, 480000 + 1000 * i, cid, datetime.now(UTC) - timedelta(days=5)))
        claims = {}
        for key, doc, pred, num, text, conf in [("a_units", d_a, "project.units", 320, None, 0.95), ("b_units", d_b, "project.units", 320, None, 0.9),
                                                 ("a_status", d_a, "project.status", None, "approved", 0.9)]:
            cur.execute("""INSERT INTO claims(subject_entity, predicate, value_num, value_text, geo_key, valid_from, observed_at, document_id, excerpt,
                           extractor_version, source_reliability, extraction_conf, confidence, half_life_days, attrs)
                           VALUES (%s,%s,%s,%s,'zcta:99001','2026-10-01',now() - interval '2 days',%s,'The council approved a 320-unit complex','extract@h',0.7,%s,0.65,180,'{"domain":"housing"}') RETURNING id""",
                        (e_proj, pred, num, text, doc, conf))
            cid = cur.fetchone()["id"]
            claims[key] = cid
            cur.execute("INSERT INTO claim_geo(claim_id, geo_key, level) SELECT %s, geo_key, level FROM geo_units WHERE geo_key = 'zcta:99001' UNION SELECT %s, ancestor_key, level FROM geo_ancestry WHERE geo_key = 'zcta:99001'", (cid, cid))
    conn.commit()
    seeds = {"tst-thesis": {"region": "tst", "geo_key": "zcta:99001", "domain": "housing", "statement": "Prices in 99001 rise on project demand.",
                            "confidence": 0.35, "falsifiers": [{"indicator_id": "zhvi", "geo_key": "zcta:99001", "operator": "<", "threshold_rel": 0.95,
                                                                "by_months": 12, "action": "review"}]}}
    import argusvue.agents.theses as TT

    orig = TT.load_seeds
    TT.load_seeds = lambda *a, **k: seeds
    try:
        T.seed(conn)
    finally:
        TT.load_seeds = orig
    tid = T.current_versions(conn, "tst")[0]["id"]
    with conn.cursor() as cur:   # pretend the seed review happened before the claims arrived
        cur.execute("UPDATE theses SET last_reviewed_at = now() - interval '10 days' WHERE id = %s", (tid,))
        cur.execute("UPDATE thesis_versions SET created_at = now() - interval '10 days' WHERE thesis_id = %s", (tid,))
    conn.commit()
    return {"conn": conn, "tid": tid, "claims": claims}


def _proposal(world, **over) -> dict:
    c = world["claims"]
    base = {"action": "update", "statement": "Prices in 99001 rise on project demand from approved multifamily projects.", "status": "emerging",
            "confidence": 0.5, "confidence_reasoning": {"independence": "two origins", "recency": "fresh", "corroboration": "unit count agrees",
                                                        "mechanism": "demand", "base_rate": "early"},
            "support": [c["a_units"], c["b_units"]], "counter": [], "causal_edges": [],
            "implications": [{"text": "More units may be approved nearby.", "cites": [c["a_status"]], "horizon_months": 6, "hypothesis": False}],
            "falsifiers": [{"indicator_id": "zhvi", "geo_key": "zcta:99001", "operator": "<", "threshold": 400000, "by_date": "2027-12-31",
                            "action": "review", "description": "values fall"}],
            "open_questions": [{"question": "Are the units under construction?", "collector_task": {"objective": "x", "kind": "search"}}],
            "change_summary": "Adds two corroborating claims on the approved 320-unit project; raises confidence from the seed level."}
    base.update(over)
    return base


def _critic(verdict: str, **over) -> dict:
    base = {"rival_mechanisms": ["general metro appreciation"], "base_rate_check": "within base rate", "source_audit": {"independence": "two origins"},
            "missing_evidence": ["rent data"], "overreach": [], "consistency": "ok", "verdict": verdict,
            "edits": [{"field": "confidence", "change": "lower to 0.4", "reason": "thin"}] if verdict == "revise" else [],
            "collector_tasks": [{"question": "Find rent data for 99001", "collector_task": {"objective": "rents 99001", "kind": "search"}}], "summary": f"{verdict} it"}
    base.update(over)
    return base


def test_golden_analyst_output_has_the_required_shape():
    data = json.loads((GOLDEN / "analyst_output.json").read_text())
    p = AnalystProposal.model_validate(data)
    assert p.action == "update" and p.falsifiers and p.change_summary and p.confidence_reasoning.base_rate
    assert any(i.hypothesis for i in p.implications) and all(i.cites or i.hypothesis for i in p.implications)


def test_validator_rejects_out_of_allowlist_ids_and_other_rules(world):
    conn, tid, c = world["conn"], world["tid"], world["claims"]
    packet = build_packet(conn, tid)
    assert set(c.values()) <= packet.allowlist_claims and "zhvi@zcta:99001" in packet.allowlist_indicators
    bad = AnalystProposal.model_validate(_proposal(world, support=[c["a_units"], 999999]))
    v = validate(conn, packet, bad, check_entailment=False)
    assert any("outside the allowlist" in e for e in v.errors)
    v = validate(conn, packet, AnalystProposal.model_validate(_proposal(world, falsifiers=[{"indicator_id": "nope", "geo_key": "zcta:99001", "operator": "<", "threshold": 1, "by_date": "2027-01-01", "action": "review"}])), check_entailment=False)
    assert any("does not exist" in e for e in v.errors)
    v = validate(conn, packet, AnalystProposal.model_validate(_proposal(world, confidence=0.8, support=[c["a_units"], c["a_status"]])), check_entailment=False)
    assert any("distinct origins" in e for e in v.errors)
    v = validate(conn, packet, AnalystProposal.model_validate(_proposal(world, falsifiers=[])), check_entailment=False)
    assert any("no new falsifier" in e for e in v.errors)
    v = validate(conn, packet, AnalystProposal.model_validate(_proposal(world, change_summary="Prices in 99001 rise on project demand from approved multifamily projects.")), check_entailment=False)
    assert any("restates" in e for e in v.errors)
    v = validate(conn, packet, AnalystProposal.model_validate(_proposal(world, implications=[{"text": "Something", "cites": [], "horizon_months": 1, "hypothesis": False}])), check_entailment=False)
    assert any("cites nothing" in e for e in v.errors)
    good = validate(conn, packet, AnalystProposal.model_validate(_proposal(world)), check_entailment=False)
    assert good.ok


def test_entailment_contradiction_is_a_hard_error(world, tmp_path):
    conn, tid = world["conn"], world["tid"]
    packet = build_packet(conn, tid)
    client = ScriptedClient()
    client.by_role["entailment"] = [_resp({"label": "contradicted", "reason": "says denied"}, model_cls=EntailmentVerdict)]
    v = validate(conn, packet, AnalystProposal.model_validate(_proposal(world)), client=client, store=BlobStore(tmp_path))
    assert any("contradicted" in e for e in v.errors) and v.entailment[0]["label"] == "contradicted"


def test_triage_not_material_stops_without_analyst(world, tmp_path):
    conn, tid = world["conn"], world["tid"]
    client = ScriptedClient()
    client.by_role["triage"] = [_resp({"material": False, "reason": "noise"}, model_cls=TriageVerdict)]
    o = review_thesis(conn, tid, trigger="weekly", client=client, store=BlobStore(tmp_path))
    assert o.stopped_at == "triage" and o.material is False and o.version is None
    with conn.cursor() as cur:
        cur.execute("SELECT kind FROM thesis_events WHERE thesis_id = %s ORDER BY id", (tid,))
        assert [r["kind"] for r in cur.fetchall()] == ["seed", "packet", "triage"]


def test_critic_approve_writes_version_and_questions(world, tmp_path):
    conn, tid = world["conn"], world["tid"]
    client = ScriptedClient()
    client.by_role["triage"] = [_resp({"material": True, "reason": "new claims"}, model_cls=TriageVerdict)]
    client.by_role["analyst"] = [_resp(_proposal(world), model_cls=AnalystProposal)]
    client.by_role["entailment"] = [_resp({"label": "entailed", "reason": "ok"}, model_cls=EntailmentVerdict)]
    client.by_role["critic"] = [_resp(_critic("approve"), model_cls=CriticReview)]
    o = review_thesis(conn, tid, trigger="claims", client=client, store=BlobStore(tmp_path))
    assert o.stopped_at == "gate" and o.critic_rounds == ["approve"] and o.version == 2 and o.open_questions == 2
    d = T.version_detail(conn, tid)
    assert d["version"]["version"] == 2 and float(d["version"]["confidence"]) == 0.5 and d["version"]["critic_verdict"] == "approve"
    assert sorted(c["claim_id"] for c in d["claims"]) == sorted([world["claims"]["a_units"], world["claims"]["b_units"]])
    assert len(d["falsifiers"]) == 1 and d["falsifiers"][0]["baseline"]["set_by"] == "analyst"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM open_questions WHERE status = 'open'")
        assert cur.fetchone()["n"] == 2
        cur.execute("SELECT kind FROM thesis_events WHERE thesis_id = %s ORDER BY id", (tid,))
        kinds = [r["kind"] for r in cur.fetchall()]
    assert kinds == ["seed", "packet", "triage", "analyst", "validator", "critic", "gate"]
    assert d["thesis"]["human_status"] == "pending" and d["thesis"]["approved_version"] == 2   # same status: no human gate


def test_critic_revise_twice_caps_confidence_and_records_dispute(world, tmp_path):
    conn, tid = world["conn"], world["tid"]
    client = ScriptedClient()
    client.by_role["triage"] = [_resp({"material": True, "reason": "new evidence"}, model_cls=TriageVerdict)]
    client.by_role["analyst"] = [_resp(_proposal(world, confidence=0.6), model_cls=AnalystProposal),
                                 _resp(_proposal(world, confidence=0.58, change_summary="Adds two claims and lowers confidence per the critic's note on thin rent evidence."), model_cls=AnalystProposal)]
    client.by_role["entailment"] = [_resp({"label": "neutral", "reason": "meh"}, model_cls=EntailmentVerdict)] * 2
    client.by_role["critic"] = [_resp(_critic("revise"), model_cls=CriticReview), _resp(_critic("revise"), model_cls=CriticReview)]
    o = review_thesis(conn, tid, trigger="anomaly", client=client, store=BlobStore(tmp_path))
    assert o.critic_rounds == ["revise", "revise"] and o.version == 2
    v = T.version_detail(conn, tid)["version"]
    assert float(v["confidence"]) == 0.5 and v["critic_verdict"] == "revise"
    assert any(dsp.get("from") == "critic" for dsp in v["known_disputes"]) and any("neutral" in str(dsp) for dsp in v["known_disputes"])
    analyst_calls = [kw for role, kw in client.calls if role == "analyst"]
    assert len(analyst_calls) == 2 and "revisions" in analyst_calls[1]["messages"][0]["content"]   # round 2 carried the edits


def test_critic_reject_writes_no_version_but_pushes_questions(world, tmp_path):
    conn, tid = world["conn"], world["tid"]
    client = ScriptedClient()
    client.by_role["triage"] = [_resp({"material": True, "reason": "new evidence"}, model_cls=TriageVerdict)]
    client.by_role["analyst"] = [_resp(_proposal(world), model_cls=AnalystProposal)]
    client.by_role["entailment"] = [_resp({"label": "entailed", "reason": "ok"}, model_cls=EntailmentVerdict)]
    client.by_role["critic"] = [_resp(_critic("reject"), model_cls=CriticReview)]
    o = review_thesis(conn, tid, trigger="manual", client=client, store=BlobStore(tmp_path))
    assert o.critic_rounds == ["reject"] and o.version is None and o.open_questions == 2
    assert T.version_detail(conn, tid)["thesis"]["current_version"] == 1


def test_validator_failure_twice_stops_loudly(world, tmp_path):
    conn, tid = world["conn"], world["tid"]
    client = ScriptedClient()
    client.by_role["triage"] = [_resp({"material": True, "reason": "new evidence"}, model_cls=TriageVerdict)]
    bad = _proposal(world, support=[999999])
    client.by_role["analyst"] = [_resp(bad, model_cls=AnalystProposal), _resp(bad, model_cls=AnalystProposal)]
    o = review_thesis(conn, tid, trigger="manual", client=client, store=BlobStore(tmp_path))
    assert o.stopped_at == "validator" and o.version is None and not client.by_role["critic"] if "critic" in client.by_role else True
    assert "prior" not in client.calls[1][1] and "failed validation" in client.calls[2][1]["messages"][0]["content"]


def test_replay_as_of_excludes_later_evidence_and_writes_nothing(world, tmp_path):
    from argusvue.eval.replay import replay

    conn, tid = world["conn"], world["tid"]
    client = ScriptedClient()
    client.by_role["analyst"] = [_resp(_proposal(world, action="no_change", change_summary="nothing new", support=[], implications=[],
                                                  open_questions=[]), model_cls=AnalystProposal)]
    before = replay(conn, tid, date.today() - timedelta(days=30), client=client, store=BlobStore(tmp_path))
    assert before["packet"]["claims"] == 0 and before["packet"]["indicators"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM thesis_events WHERE thesis_id = %s AND kind <> 'seed'", (tid,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT current_version FROM theses WHERE id = %s", (tid,))
        assert cur.fetchone()["current_version"] == 1


def test_due_theses_triggers(world):
    conn, tid = world["conn"], world["tid"]
    due = due_theses(conn, "tst")
    assert due and due[0]["id"] == tid and "claims_due" in due[0]["reasons"]


def test_critic_packet_text_is_stable_across_validator_reruns():
    """The critic request must not change when the validator is re-run (run ids differ every time),
    otherwise session-mode request ids drift and answered reviews are lost."""
    from argusvue.agents.review import _entailment_line

    a = {"implication": 0, "label": "neutral", "reason": "says nothing about supply", "run_id": 251}
    b = {**a, "run_id": 274}
    assert _entailment_line(a) == _entailment_line(b)
    assert "run_id" not in _entailment_line(a) and "251" not in _entailment_line(a)

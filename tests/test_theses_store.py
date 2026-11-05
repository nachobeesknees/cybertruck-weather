from __future__ import annotations

from datetime import date

import pytest

from argusvue.agents import theses as T

pytestmark = pytest.mark.db


@pytest.fixture
def seeded(synthetic, monkeypatch):
    conn = synthetic
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sources(slug, name, kind, region_id, reliability, origin_key) VALUES ('s','S','structured','tst',0.9,'o') RETURNING id")
        sid = cur.fetchone()["id"]
        cur.execute("INSERT INTO indicators(id, name, unit, direction_good, half_life_days, source_id, region_scope, frequency) VALUES ('zhvi','Z','USD','neutral',45,%s,'{tst}','monthly')", (sid,))
        cur.execute("INSERT INTO documents(source_id, url, fetched_at, content_hash, blob_ref) VALUES (%s,'u',now(),decode('aa','hex'),'b') RETURNING id", (sid,))
        did = cur.fetchone()["id"]
        cur.execute("INSERT INTO entities(type, canonical_name, region_id, geo_key, status) VALUES ('place','z','tst','zcta:99001','canonical') RETURNING id")
        eid = cur.fetchone()["id"]
        for i, (p, v) in enumerate([("2026-07-01", 500000), ("2026-08-01", 505000), ("2026-09-01", 510000)]):
            cur.execute("""INSERT INTO claims(subject_entity, predicate, value_num, geo_key, valid_from, observed_at, document_id, excerpt,
                           extractor_version, source_reliability, extraction_conf, confidence) VALUES (%s,'indicator:zhvi',%s,'zcta:99001',%s,now(),%s,'r','structured@1',0.9,1,0.9) RETURNING id""",
                        (eid, v, p, did))
            cid = cur.fetchone()["id"]
            cur.execute("INSERT INTO indicator_points(indicator_id, geo_key, period, value, claim_id, observed_at) VALUES ('zhvi','zcta:99001',%s,%s,%s,now())", (p, v, cid))
    conn.commit()
    seeds = {"tst-thesis": {"region": "tst", "geo_key": "zcta:99001", "domain": "housing", "statement": "Prices rise.", "confidence": 0.35,
                            "falsifiers": [{"indicator_id": "zhvi", "geo_key": "zcta:99001", "operator": "<", "threshold_rel": 0.95, "by_months": 12, "action": "review"}],
                            "causal_edges": [{"cause": "a", "effect": "b", "mechanism": "m", "lag_months": 3, "strength": "weak"}]}}
    monkeypatch.setattr(T, "load_seeds", lambda *a, **k: seeds)
    return conn


def test_seed_resolves_relative_falsifier_and_is_idempotent(seeded):
    conn = seeded
    r = T.seed(conn)
    assert r["created"] == ["tst-thesis"]
    assert T.seed(conn) == {"created": [], "skipped": ["tst-thesis"]}
    rows = T.current_versions(conn, "tst")
    assert len(rows) == 1 and rows[0]["status"] == "emerging" and float(rows[0]["confidence"]) == 0.35
    d = T.version_detail(conn, rows[0]["id"])
    f = d["falsifiers"][0]
    assert float(f["threshold"]) == pytest.approx(510000 * 0.95) and f["by_date"] == date(2027, 9, 30)
    assert f["baseline"]["period"] == "2026-09-01" and f["baseline"]["threshold_rel"] == 0.95
    assert d["edges"][0]["status"] == "hypothesis"
    assert d["thesis"]["human_status"] == "pending" and d["thesis"]["approved_version"] == 1


def test_seed_fails_loudly_without_data(seeded, monkeypatch):
    conn = seeded
    with pytest.raises(ValueError, match="no data"):
        T.resolve_falsifier(conn, T.FalsifierSpec(indicator_id="zhvi", geo_key="zcta:99002", operator="<", action="review", threshold_rel=0.9))
    with pytest.raises(ValueError, match="unknown indicator"):
        T.resolve_falsifier(conn, T.FalsifierSpec(indicator_id="nope", geo_key="zcta:99001", operator="<", action="review", threshold=1))


def test_status_change_needs_human_approval_and_reject_reverts(seeded):
    conn = seeded
    T.seed(conn)
    tid = T.current_versions(conn, "tst")[0]["id"]
    # same-status update: current advances; approved follows because v1 was the approved pointer
    v2 = T.write_version(conn, tid, statement="Prices rise faster.", status="emerging", confidence=0.4, confidence_reasoning={},
                         implications=[], change_summary="new data", support=[], counter=[], edges=[], falsifiers=[])
    t = T.version_detail(conn, tid)["thesis"]
    assert (v2, t["current_version"], t["approved_version"], t["human_status"]) == (2, 2, 2, "pending")
    # status change -> pending human; rendered stays v2
    v3 = T.write_version(conn, tid, statement="Prices established.", status="established", confidence=0.65, confidence_reasoning={},
                         implications=[], change_summary="corroborated", support=[], counter=[], edges=[], falsifiers=[])
    cur_row = T.current_versions(conn, "tst")[0]
    assert v3 == 3 and cur_row["rendered_version"] == 2 and cur_row["pending_human"] is True and cur_row["status"] == "emerging"
    T.decide(conn, tid, "approved", "looks right")
    cur_row = T.current_versions(conn, "tst")[0]
    assert cur_row["rendered_version"] == 3 and cur_row["status"] == "established" and not cur_row["pending_human"]
    v4 = T.write_version(conn, tid, statement="Dead.", status="dead", confidence=0.1, confidence_reasoning={}, implications=[],
                         change_summary="falsified", support=[], counter=[], edges=[], falsifiers=[])
    T.decide(conn, tid, "rejected", "disagree")
    t = T.version_detail(conn, tid)["thesis"]
    assert v4 == 4 and t["current_version"] == 3 and t["approved_version"] == 3 and t["human_status"] == "rejected"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM thesis_versions WHERE thesis_id = %s", (tid,))
        assert cur.fetchone()["n"] == 4      # nothing deleted
        cur.execute("SELECT decision FROM review_decisions WHERE thesis_id = %s ORDER BY id", (tid,))
        assert [r["decision"] for r in cur.fetchall()] == ["approved", "rejected"]

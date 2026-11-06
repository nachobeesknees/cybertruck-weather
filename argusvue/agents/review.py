"""Thesis review loop (SPEC §8): Triage -> Analyst -> Validator -> Critic -> Gate, fully logged.

Every step writes a thesis_events row with its agent_run id and payload. Versions are written only through
agents.theses.write_version (immutable). `dry_run` (replay) runs the loop without writing versions, events
or open questions, and returns the proposal and verdicts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from argusvue import llm
from argusvue.agents import theses as T
from argusvue.agents.packet import Packet, build_packet, delta_is_empty
from argusvue.agents.schemas import AnalystProposal, CriticReview, TriageVerdict
from argusvue.agents.validator import Validation, validate
from argusvue.settings import PACKAGE_ROOT
from argusvue.store.blobs import BlobStore
from argusvue.store.jobs import job_run

log = logging.getLogger(__name__)
PROMPTS = PACKAGE_ROOT / "agents" / "prompts"
MAX_ANALYST_ROUNDS = 2
DISPUTE_CONFIDENCE_CAP = 0.5


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


@dataclass
class ReviewOutcome:
    thesis_id: int
    trigger: str
    stopped_at: str                       # triage | validator | gate
    material: bool | None = None
    proposal: AnalystProposal | None = None
    validation: Validation | None = None
    critic: CriticReview | None = None
    critic_rounds: list[str] = field(default_factory=list)
    version: int | None = None
    disputes: list[dict] = field(default_factory=list)
    open_questions: int = 0
    cost_usd: float = 0.0
    run_ids: list[int] = field(default_factory=list)
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"thesis_id": self.thesis_id, "trigger": self.trigger, "stopped_at": self.stopped_at, "material": self.material,
                "action": self.proposal.action if self.proposal else None, "status": self.proposal.status if self.proposal else None,
                "confidence": self.proposal.confidence if self.proposal else None, "critic_rounds": self.critic_rounds,
                "version": self.version, "disputes": len(self.disputes), "open_questions": self.open_questions,
                "cost_usd": round(self.cost_usd, 4), "runs": self.run_ids, "dry_run": self.dry_run, "notes": self.notes}


def _event(conn, thesis_id: int, kind: str, payload: dict, *, run_id: int | None = None, version: int | None = None,
           dry_run: bool = False) -> None:
    if dry_run:
        return
    with conn.cursor() as cur:
        cur.execute("INSERT INTO thesis_events(thesis_id, version, kind, run_id, payload) VALUES (%s,%s,%s,%s,%s)",
                    (thesis_id, version, kind, run_id, Jsonb(json.loads(json.dumps(payload, default=str)))))
    conn.commit()


def _analyst_user(packet: Packet, extra: str = "") -> str:
    return (f"Review this thesis and return a proposal as JSON matching the schema.\n{extra}\n\nPACKET:\n{packet.to_prompt()}\n\n"
            f"SCHEMA:\n{json.dumps(AnalystProposal.model_json_schema())}")


def _entailment_line(e: dict) -> str:
    """Stable text for the critic packet: no run ids, so re-running the validator never changes the critic request."""
    return f"entailment check, implication {e['implication']}: {e['label']} ({e['reason']})"


def _critic_user(packet: Packet, proposal: AnalystProposal, validation: Validation) -> str:
    return (f"PROPOSAL:\n{proposal.model_dump_json(indent=1)}\n\nVALIDATOR FLAGS (code checks, informational):\n"
            f"{json.dumps(validation.flags + [_entailment_line(e) for e in validation.entailment])}\n\nPACKET:\n{packet.to_prompt()}\n\n"
            f"SCHEMA:\n{json.dumps(CriticReview.model_json_schema())}")


def review_thesis(conn: psycopg.Connection, thesis_id: int, *, trigger: str = "manual", force: bool = False,
                  as_of: datetime | None = None, dry_run: bool = False, client=None, store: BlobStore | None = None) -> ReviewOutcome:
    store = store or BlobStore()
    out = ReviewOutcome(thesis_id=thesis_id, trigger=trigger, stopped_at="triage", dry_run=dry_run)
    if as_of is None:   # a stable cutoff so re-runs within the day rebuild the same packet (session mode)
        now = datetime.now(UTC)
        as_of = now.replace(hour=23, minute=59, second=59, microsecond=0)
    packet = build_packet(conn, thesis_id, as_of=as_of)
    packet_ref = store.put_bytes(packet.to_prompt().encode(), suffix=".json").ref
    _event(conn, thesis_id, "packet", {"trigger": trigger, "packet_ref": packet_ref, "claims": len(packet.claims),
                                       "indicators": len(packet.indicators), "as_of": packet.as_of}, dry_run=dry_run)

    # 1. Triage (haiku)
    if not force:
        if delta_is_empty(packet) and trigger not in ("manual", "seed_review"):
            out.material = False
            _event(conn, thesis_id, "triage", {"material": False, "reason": "empty delta (no LLM call)"}, dry_run=dry_run)
            return out
        tri = llm.complete(conn, "triage", system=prompt("triage"), user=f"THESIS AND DELTA:\n{packet.to_prompt()}",
                           schema=TriageVerdict, region_id=packet.region_id, geo_key=packet.geo_key, client=client, store=store,
                           max_tokens=400)
        out.run_ids.append(tri.run_id)
        out.cost_usd += tri.cost_usd
        out.material = tri.parsed.material
        _event(conn, thesis_id, "triage", {"material": tri.parsed.material, "reason": tri.parsed.reason}, run_id=tri.run_id, dry_run=dry_run)
        if not tri.parsed.material:
            return out
    else:
        out.material = True
        _event(conn, thesis_id, "triage", {"material": True, "reason": "forced"}, dry_run=dry_run)

    # 2-4. Analyst -> Validator -> Critic, up to MAX_ANALYST_ROUNDS
    extra = ""
    proposal: AnalystProposal | None = None
    validation: Validation | None = None
    critic: CriticReview | None = None
    for round_no in range(1, MAX_ANALYST_ROUNDS + 1):
        res = llm.complete(conn, "analyst", system=prompt("analyst"), user=_analyst_user(packet, extra), schema=AnalystProposal,
                           region_id=packet.region_id, geo_key=packet.geo_key, client=client, store=store, max_tokens=12000)
        out.run_ids.append(res.run_id)
        out.cost_usd += res.cost_usd
        proposal = res.parsed
        _event(conn, thesis_id, "analyst", {"round": round_no, "action": proposal.action, "status": proposal.status,
                                            "confidence": proposal.confidence}, run_id=res.run_id, dry_run=dry_run)
        validation = validate(conn, packet, proposal, client=client, store=store)
        if not validation.ok:   # re-prompt once with the error list
            res2 = llm.complete(conn, "analyst", system=prompt("analyst"), user=_analyst_user(packet, extra), schema=AnalystProposal,
                                region_id=packet.region_id, geo_key=packet.geo_key, client=client, store=store, max_tokens=12000,
                                prior_errors=validation.errors)
            out.run_ids.append(res2.run_id)
            out.cost_usd += res2.cost_usd
            proposal = res2.parsed
            validation = validate(conn, packet, proposal, client=client, store=store)
            _event(conn, thesis_id, "analyst", {"round": round_no, "reprompt": True, "action": proposal.action, "status": proposal.status,
                                                "confidence": proposal.confidence}, run_id=res2.run_id, dry_run=dry_run)
        _event(conn, thesis_id, "validator", {"round": round_no, "ok": validation.ok, "errors": validation.errors, "flags": validation.flags,
                                              "entailment": validation.entailment}, dry_run=dry_run)
        out.proposal, out.validation = proposal, validation
        if not validation.ok:
            out.stopped_at = "validator"
            out.notes.append(f"validator rejected the proposal twice in round {round_no}: {validation.errors}")
            return out
        if proposal.action == "no_change":
            out.stopped_at = "gate"
            _event(conn, thesis_id, "gate", {"verdict": "no_change", "round": round_no}, dry_run=dry_run)
            return out
        cres = llm.complete(conn, "critic", system=prompt("critic"), user=_critic_user(packet, proposal, validation), schema=CriticReview,
                            region_id=packet.region_id, geo_key=packet.geo_key, client=client, store=store, max_tokens=6000)
        out.run_ids.append(cres.run_id)
        out.cost_usd += cres.cost_usd
        critic = cres.parsed
        out.critic = critic
        out.critic_rounds.append(critic.verdict)
        _event(conn, thesis_id, "critic", {"round": round_no, "verdict": critic.verdict, "edits": [e.model_dump() for e in critic.edits],
                                           "missing_evidence": critic.missing_evidence, "overreach": critic.overreach,
                                           "summary": critic.summary}, run_id=cres.run_id, dry_run=dry_run)
        if critic.verdict == "approve":
            break
        if critic.verdict == "reject":
            break
        # revise: one more analyst round with the critic's edits
        extra = ("The critic asked for revisions. Apply these edits (or explain in change_summary why not):\n"
                 + "\n".join(f"- {e.field}: {e.change} ({e.reason})" for e in critic.edits)
                 + "\nMissing evidence noted: " + "; ".join(critic.missing_evidence))

    # 5. Gate
    out.stopped_at = "gate"
    assert proposal is not None and critic is not None and validation is not None
    disputes: list[dict] = []
    if critic.verdict == "reject":
        _event(conn, thesis_id, "gate", {"verdict": "reject", "reason": critic.summary}, dry_run=dry_run)
        out.open_questions = _push_questions(conn, packet, proposal, critic, out.run_ids[-1] if out.run_ids else None, dry_run)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute("UPDATE theses SET last_reviewed_at = now() WHERE id = %s", (thesis_id,))
            conn.commit()
        return out
    confidence = proposal.confidence
    if critic.verdict == "revise":   # unresolved after the allowed rounds: cap confidence, store objections
        disputes = [{"from": "critic", "verdict": "revise", "edits": [e.model_dump() for e in critic.edits],
                     "missing_evidence": critic.missing_evidence, "overreach": critic.overreach, "summary": critic.summary,
                     "recorded_at": datetime.now(UTC).isoformat()}]
        confidence = min(confidence, DISPUTE_CONFIDENCE_CAP)
    for f in validation.flags:
        disputes.append({"from": "validator", "flag": f})
    out.disputes = disputes
    status = "dead" if proposal.action == "retire" else proposal.status
    if dry_run:
        out.notes.append("dry run: no version written")
        return out
    edges = [T.EdgeSpec(cause=e.cause, effect=e.effect, mechanism=e.mechanism, lag_months=e.lag_months, strength=e.strength,
                        evidence=e.evidence, cause_indicator=e.cause_indicator, effect_indicator=e.effect_indicator, geo_key=e.geo_key)
             for e in proposal.causal_edges]
    fals = [{"indicator_id": f.indicator_id, "geo_key": f.geo_key, "operator": f.operator, "threshold": f.threshold,
             "by_date": date.fromisoformat(f.by_date), "action": f.action, "description": f.description,
             "baseline": {"set_by": "analyst", "run_id": out.run_ids[-1]}} for f in proposal.falsifiers]
    implications = [i.model_dump() for i in proposal.implications]
    version = T.write_version(conn, thesis_id, statement=proposal.statement, status=status, confidence=confidence,
                              confidence_reasoning=proposal.confidence_reasoning.model_dump(), implications=implications,
                              change_summary=proposal.change_summary, support=proposal.support, counter=proposal.counter, edges=edges,
                              falsifiers=fals, analyst_run_id=next((r for r in out.run_ids), None), critic_run_id=out.run_ids[-1],
                              critic_verdict=critic.verdict, known_disputes=disputes, trigger=trigger)
    out.version = version
    _event(conn, thesis_id, "gate", {"verdict": critic.verdict, "version": version, "confidence": confidence,
                                     "capped": critic.verdict == "revise", "action": proposal.action}, version=version)
    out.open_questions = _push_questions(conn, packet, proposal, critic, out.run_ids[-1], dry_run)
    conn.commit()
    return out


def _push_questions(conn, packet: Packet, proposal: AnalystProposal, critic: CriticReview, run_id: int | None, dry_run: bool) -> int:
    qs = [(q.question, q.collector_task) for q in proposal.open_questions] + [(q.question, q.collector_task) for q in critic.collector_tasks]
    if dry_run or not qs:
        return len(qs)
    with conn.cursor() as cur:
        for question, task in qs:
            cur.execute("""INSERT INTO open_questions(region_id, geo_key, question, suggested_collector_task, raised_by_run)
                           VALUES (%s, %s, %s, %s, %s)""", (packet.region_id, packet.geo_key, question, Jsonb(task or {}), run_id))
    conn.commit()
    return len(qs)


# ---- triggers -------------------------------------------------------------------------------------
def due_theses(conn: psycopg.Connection, region_id: str | None = None, *, weekly_days: int = 7) -> list[dict]:
    """Theses that need a review: weekly schedule, triggered falsifier, new/superseded claims in scope, anomalies."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT t.id, t.slug, t.region_id, t.geo_key, t.domain, t.last_reviewed_at, r.cbsa_code,
                      (t.last_reviewed_at IS NULL OR t.last_reviewed_at < now() - make_interval(days => %s)) AS weekly_due,
                      EXISTS (SELECT 1 FROM thesis_events e WHERE e.thesis_id = t.id AND e.kind = 'falsifier_triggered'
                              AND e.created_at > coalesce(t.last_reviewed_at, '1970-01-01')) AS falsifier_due,
                      EXISTS (SELECT 1 FROM claims c JOIN claim_geo g ON g.claim_id = c.id
                              WHERE g.geo_key = t.geo_key AND c.extractor_version LIKE 'extract@%%' AND c.attrs->>'domain' = t.domain
                                AND c.created_at > coalesce(t.last_reviewed_at, '1970-01-01')) AS claims_due,
                      EXISTS (SELECT 1 FROM claims c JOIN thesis_claims tc ON tc.claim_id = c.id
                              WHERE tc.thesis_id = t.id AND c.status = 'superseded'
                                AND c.superseded_by IN (SELECT id FROM claims WHERE created_at > coalesce(t.last_reviewed_at, '1970-01-01'))) AS superseded_due,
                      EXISTS (SELECT 1 FROM indicator_anomalies a WHERE a.geo_key IN (t.geo_key, 'cbsa:' || r.cbsa_code)
                              AND a.computed_at > coalesce(t.last_reviewed_at, '1970-01-01') AND abs(coalesce(a.zscore, 0)) >= 2.5) AS anomaly_due
               FROM theses t JOIN regions r ON r.id = t.region_id
               WHERE t.active AND (%s::text IS NULL OR t.region_id = %s) ORDER BY t.id""",
            (weekly_days, region_id, region_id),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        reasons = [k for k in ("falsifier_due", "claims_due", "superseded_due", "anomaly_due", "weekly_due") if r[k]]
        if reasons:
            out.append({**dict(r), "reasons": reasons, "trigger": reasons[0].replace("_due", "")})
    return out


def review_due(conn: psycopg.Connection, region_id: str | None = None, *, client=None, store=None, limit: int = 20) -> list[dict]:
    results = []
    for t in due_theses(conn, region_id)[:limit]:
        try:
            with job_run(conn, "theses.review", t["region_id"]) as run:
                o = review_thesis(conn, t["id"], trigger=t["trigger"], client=client, store=store)
                run.stats.update(o.summary())
                results.append(o.summary())
        except llm.SpendCapExceeded as e:
            results.append({"thesis_id": t["id"], "error": str(e)})
            break
        except llm.PendingAnswer as e:
            results.append({"thesis_id": t["id"], "pending": e.request_id})
            continue
        except Exception as e:  # keep reviewing the others; job_runs has the traceback
            log.exception("review of thesis %s failed", t["id"])
            results.append({"thesis_id": t["id"], "error": f"{type(e).__name__}: {e}"})
    return results

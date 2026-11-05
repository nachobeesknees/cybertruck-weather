"""Thesis store: seeds, versions, evidence links, falsifiers, human gate. Versions are immutable; the
`theses` row only moves pointers (current_version, approved_version, human_status)."""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb

from argusvue.settings import CONFIG_DIR

log = logging.getLogger(__name__)


@dataclass
class FalsifierSpec:
    indicator_id: str
    geo_key: str
    operator: str
    action: str
    by_date: date | None = None
    threshold: float | None = None
    threshold_rel: float | None = None
    threshold_abs: float | None = None
    by_months: int | None = None
    description: str | None = None


@dataclass
class EdgeSpec:
    cause: str
    effect: str
    mechanism: str
    lag_months: int = 0
    strength: str = "moderate"
    evidence: list[int] = field(default_factory=list)
    cause_indicator: str | None = None
    effect_indicator: str | None = None
    geo_key: str | None = None


@lru_cache
def load_seeds(path: Path = CONFIG_DIR / "theses_seed.yaml") -> dict[str, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out = {}
    for slug, body in (raw.get("theses") or {}).items():
        body = dict(body)
        if float(body.get("confidence", 0.4)) > 0.4:
            raise ValueError(f"seed {slug}: confidence must be <= 0.4")
        if not body.get("falsifiers"):
            raise ValueError(f"seed {slug}: at least one falsifier is required")
        out[slug] = body
    return out


def latest_point(conn: psycopg.Connection, indicator_id: str, geo_key: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT period, value FROM indicator_latest WHERE indicator_id = %s AND geo_key = %s ORDER BY period DESC LIMIT 1",
                    (indicator_id, geo_key))
        return cur.fetchone()


def resolve_falsifier(conn: psycopg.Connection, spec: FalsifierSpec, *, as_of: date | None = None) -> dict:
    """Turn a relative spec into an absolute, machine-checkable falsifier. Fails loudly when the indicator
    or geo has no data (SPEC §7: every seed falsifier must be checkable)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM indicators WHERE id = %s", (spec.indicator_id,))
        if cur.fetchone() is None:
            raise ValueError(f"falsifier references unknown indicator {spec.indicator_id!r}")
        cur.execute("SELECT 1 FROM geo_units WHERE geo_key = %s", (spec.geo_key,))
        if cur.fetchone() is None:
            raise ValueError(f"falsifier references unknown geo {spec.geo_key!r}")
    if spec.operator not in ("<", ">", "<=", ">="):
        raise ValueError(f"bad operator {spec.operator!r}")
    if spec.action not in ("downgrade", "retire", "review"):
        raise ValueError(f"bad action {spec.action!r}")
    baseline: dict[str, Any] = {}
    threshold = spec.threshold if spec.threshold is not None else spec.threshold_abs
    by_date = spec.by_date
    if threshold is None or by_date is None:
        lp = latest_point(conn, spec.indicator_id, spec.geo_key)
        if lp is None:
            raise ValueError(f"no data for {spec.indicator_id} @ {spec.geo_key}; cannot resolve falsifier")
        baseline = {"period": lp["period"].isoformat(), "value": float(lp["value"])}
        if threshold is None:
            if spec.threshold_rel is None:
                raise ValueError("falsifier needs threshold, threshold_abs or threshold_rel")
            threshold = round(float(lp["value"]) * float(spec.threshold_rel), 4)
            baseline["threshold_rel"] = spec.threshold_rel
        if by_date is None:
            months = spec.by_months or 12
            y, m = lp["period"].year, lp["period"].month + months
            y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
            by_date = date(y, m, calendar.monthrange(y, m)[1])   # end of the target month
            baseline["by_months"] = months
    return {"indicator_id": spec.indicator_id, "geo_key": spec.geo_key, "operator": spec.operator, "threshold": float(threshold),
            "by_date": by_date, "action": spec.action, "description": spec.description, "baseline": baseline}


def write_version(conn: psycopg.Connection, thesis_id: int, *, statement: str, status: str, confidence: float,
                  confidence_reasoning: dict, implications: list[dict], change_summary: str, support: list[int],
                  counter: list[int], edges: list[EdgeSpec], falsifiers: list[dict], analyst_run_id: int | None = None,
                  critic_run_id: int | None = None, critic_verdict: str | None = None, known_disputes: list | None = None,
                  trigger: str | None = None) -> int:
    """Append a new immutable version and move current_version. A status change puts the thesis back into
    human_status = pending (the approved pointer stays until `theses approve`)."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_version, approved_version FROM theses WHERE id = %s FOR UPDATE", (thesis_id,))
        t = cur.fetchone()
        if t is None:
            raise KeyError(f"thesis {thesis_id} not found")
        prev_status = None
        if t["current_version"]:
            cur.execute("SELECT status FROM thesis_versions WHERE thesis_id = %s AND version = %s", (thesis_id, t["approved_version"] or t["current_version"]))
            prev_status = cur.fetchone()["status"]
        cur.execute("SELECT coalesce(max(version), 0) + 1 AS v FROM thesis_versions WHERE thesis_id = %s", (thesis_id,))
        version = cur.fetchone()["v"]          # never reuse a number, even after a human rejection moved current back
        cur.execute(
            """INSERT INTO thesis_versions(thesis_id, version, statement, status, confidence, confidence_reasoning, implications,
                                           change_summary, analyst_run_id, critic_run_id, critic_verdict, known_disputes, trigger)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (thesis_id, version, statement, status, confidence, Jsonb(confidence_reasoning), Jsonb(implications), change_summary,
             analyst_run_id, critic_run_id, critic_verdict, Jsonb(known_disputes or []), trigger),
        )
        for cid in sorted(set(support)):
            cur.execute("INSERT INTO thesis_claims(thesis_id, version, claim_id, role) VALUES (%s,%s,%s,'support')", (thesis_id, version, cid))
        for cid in sorted(set(counter)):
            cur.execute("INSERT INTO thesis_claims(thesis_id, version, claim_id, role) VALUES (%s,%s,%s,'counter')", (thesis_id, version, cid))
        for e in edges:
            cur.execute(
                """INSERT INTO causal_edges(thesis_id, version, cause, effect, mechanism, lag_months, strength, evidence_claim_ids,
                                            cause_indicator, effect_indicator, geo_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (thesis_id, version, e.cause, e.effect, e.mechanism, e.lag_months, e.strength, e.evidence, e.cause_indicator,
                 e.effect_indicator, e.geo_key),
            )
        for f in falsifiers:
            cur.execute(
                """INSERT INTO falsifiers(thesis_id, version, indicator_id, geo_key, operator, threshold, by_date, action, description, baseline)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (thesis_id, version, f["indicator_id"], f["geo_key"], f["operator"], f["threshold"], f["by_date"], f["action"],
                 f.get("description"), Jsonb(f.get("baseline") or {})),
            )
        status_changed = prev_status is not None and prev_status != status
        first = t["current_version"] == 0
        if first:
            cur.execute("UPDATE theses SET current_version = %s, approved_version = %s, human_status = 'pending', last_reviewed_at = now() WHERE id = %s",
                        (version, version, thesis_id))
        elif status_changed:
            cur.execute("UPDATE theses SET current_version = %s, human_status = 'pending', last_reviewed_at = now() WHERE id = %s",
                        (version, thesis_id))
        else:
            cur.execute("""UPDATE theses SET current_version = %s, last_reviewed_at = now(),
                             approved_version = CASE WHEN human_status = 'approved' OR approved_version = current_version THEN %s ELSE approved_version END
                           WHERE id = %s""", (version, version, thesis_id))
    return version


def seed(conn: psycopg.Connection, *, region_id: str | None = None) -> dict[str, Any]:
    """Create seed theses that do not exist yet (idempotent: existing slugs are left untouched)."""
    created, skipped = [], []
    for slug, body in load_seeds().items():
        if region_id and body["region"] != region_id:
            continue
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM theses WHERE slug = %s", (slug,))
            if cur.fetchone():
                skipped.append(slug)
                continue
            cur.execute("SELECT 1 FROM geo_units WHERE geo_key = %s", (body["geo_key"],))
            if cur.fetchone() is None:
                raise ValueError(f"seed {slug}: geo {body['geo_key']} not in gazetteer")
            cur.execute("INSERT INTO theses(slug, region_id, geo_key, domain) VALUES (%s,%s,%s,%s) RETURNING id",
                        (slug, body["region"], body["geo_key"], body["domain"]))
            tid = cur.fetchone()["id"]
        fals = [resolve_falsifier(conn, FalsifierSpec(**f)) for f in body["falsifiers"]]
        edges = [EdgeSpec(**e) for e in body.get("causal_edges", [])]
        implications = [{**i, "cites": i.get("cites", []), "hypothesis": not i.get("cites")} for i in body.get("implications", [])]
        write_version(conn, tid, statement=body["statement"].strip(), status="emerging", confidence=float(body.get("confidence", 0.4)),
                      confidence_reasoning={"seed": "operator-authored seed; no claims cited yet"}, implications=implications,
                      change_summary="Seed thesis (SPEC §7).", support=[], counter=[], edges=edges, falsifiers=fals,
                      critic_verdict="seed", trigger="seed")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO thesis_events(thesis_id, version, kind, payload) VALUES (%s, 1, 'seed', %s)",
                        (tid, Jsonb({"falsifiers": len(fals), "edges": len(edges)})))
        created.append(slug)
    conn.commit()
    return {"created": created, "skipped": skipped}


def decide(conn: psycopg.Connection, thesis_id: int, decision: str, note: str | None = None) -> dict:
    """Human gate: approve moves approved_version to current_version; reject points current back to approved."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved|rejected")
    with conn.cursor() as cur:
        cur.execute("SELECT current_version, approved_version FROM theses WHERE id = %s FOR UPDATE", (thesis_id,))
        t = cur.fetchone()
        if t is None:
            raise KeyError(f"thesis {thesis_id} not found")
        version = t["current_version"]
        cur.execute("INSERT INTO review_decisions(thesis_id, version, decision, note) VALUES (%s,%s,%s,%s)", (thesis_id, version, decision, note))
        if decision == "approved":
            cur.execute("UPDATE theses SET human_status = 'approved', approved_version = current_version WHERE id = %s", (thesis_id,))
        else:
            cur.execute("UPDATE theses SET human_status = 'rejected', current_version = approved_version WHERE id = %s", (thesis_id,))
        cur.execute("INSERT INTO thesis_events(thesis_id, version, kind, payload) VALUES (%s, %s, 'gate_human', %s)",
                    (thesis_id, version, Jsonb({"decision": decision, "note": note})))
    conn.commit()
    return {"thesis_id": thesis_id, "version": version, "decision": decision}


def current_versions(conn: psycopg.Connection, region_id: str | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""SELECT * FROM thesis_current WHERE %s::text IS NULL OR region_id = %s ORDER BY region_id, slug""", (region_id, region_id))
        return cur.fetchall()


def version_detail(conn: psycopg.Connection, thesis_id: int, version: int | None = None) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM theses WHERE id = %s", (thesis_id,))
        t = cur.fetchone()
        if t is None:
            raise KeyError(f"thesis {thesis_id} not found")
        v = version or t["current_version"]
        cur.execute("SELECT * FROM thesis_versions WHERE thesis_id = %s AND version = %s", (thesis_id, v))
        ver = cur.fetchone()
        cur.execute("SELECT claim_id, role FROM thesis_claims WHERE thesis_id = %s AND version = %s ORDER BY role, claim_id", (thesis_id, v))
        claims = cur.fetchall()
        cur.execute("SELECT * FROM causal_edges WHERE thesis_id = %s AND version = %s ORDER BY id", (thesis_id, v))
        edges = cur.fetchall()
        cur.execute("""SELECT f.*, (SELECT triggered FROM falsifier_checks c WHERE c.falsifier_id = f.id ORDER BY checked_at DESC LIMIT 1) AS last_triggered,
                              (SELECT value FROM falsifier_checks c WHERE c.falsifier_id = f.id ORDER BY checked_at DESC LIMIT 1) AS last_value
                       FROM falsifiers f WHERE thesis_id = %s AND version = %s ORDER BY id""", (thesis_id, v))
        fals = cur.fetchall()
        cur.execute("SELECT * FROM review_decisions WHERE thesis_id = %s ORDER BY id", (thesis_id,))
        decisions = cur.fetchall()
    return {"thesis": t, "version": ver, "claims": claims, "edges": edges, "falsifiers": fals, "decisions": decisions}

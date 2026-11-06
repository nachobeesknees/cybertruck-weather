"""Reconcile new claims against the ledger (SPEC §6). Append-only: claims are never deleted; status,
corroboration_count, confidence, valid_to and superseded_by are the only fields that change."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

from argusvue.reconcile.rules import combined_confidence, decide, half_life_for
from argusvue.store.jobs import job_run

log = logging.getLogger(__name__)


@dataclass
class ReconcileStats:
    processed: int = 0
    corroborated: int = 0
    merged: int = 0
    contested: int = 0
    superseded: int = 0
    untouched: int = 0
    conflicts_opened: int = 0
    by_predicate: dict[str, int] = field(default_factory=dict)


def _origin_reliabilities(conn, claim_id: int) -> list[float]:
    """Reliability per distinct origin supporting a claim: the best reliability among the sources that
    carry that origin_key (several monitors can share a domain)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT co.origin_key, max(s.reliability) AS r
               FROM claim_origins co
               LEFT JOIN sources s ON s.origin_key = co.origin_key
               WHERE co.claim_id = %s GROUP BY co.origin_key""",
            (claim_id,),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append(float(r["r"]) if r["r"] is not None else 0.5)
    return out


def _add_origin(conn, claim_id: int, origin_key: str, reliability: float) -> bool:
    """Add a supporting origin; returns True if it was new."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO claim_origins(claim_id, origin_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (claim_id, origin_key))
        new = cur.rowcount == 1
        if new:
            cur.execute("UPDATE claims SET corroboration_count = corroboration_count + 1 WHERE id = %s", (claim_id,))
    # recompute confidence from all origins (source reliabilities per origin)
    rels = _origin_reliabilities(conn, claim_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE claims SET confidence = %s WHERE id = %s", (combined_confidence(rels), claim_id))
    return new


def _claim_origin(conn, claim_id: int) -> tuple[str, float]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT coalesce(d.origin_key, s.origin_key) AS origin_key, s.reliability
               FROM claims c JOIN documents d ON d.id = c.document_id LEFT JOIN sources s ON s.id = d.source_id
               WHERE c.id = %s""",
            (claim_id,),
        )
        r = cur.fetchone()
    return r["origin_key"] or "unknown", float(r["reliability"] if r["reliability"] is not None else 0.5)


def reconcile_claim(conn: psycopg.Connection, claim_id: int, stats: ReconcileStats | None = None) -> str:
    """Reconcile one claim against earlier claims with the same subject + predicate. Returns the outcome."""
    stats = stats or ReconcileStats()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM claims WHERE id = %s", (claim_id,))
        new = cur.fetchone()
        if new is None:
            raise KeyError(f"claim {claim_id} not found")
        cur.execute(
            """SELECT * FROM claims WHERE subject_entity = %s AND predicate = %s AND id < %s
                 AND coalesce(object_entity, -1) = coalesce(%s, coalesce(object_entity, -1))
               ORDER BY id""",
            (new["subject_entity"], new["predicate"], claim_id, new["object_entity"]),
        )
        olds = cur.fetchall()
    new_origin, new_rel = _claim_origin(conn, claim_id)
    with conn.cursor() as cur:  # the new claim always supports itself
        cur.execute("INSERT INTO claim_origins(claim_id, origin_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (claim_id, new_origin))
    outcome = "untouched"
    for old in olds:
        d = decide(new["predicate"], new_valid_from=new["valid_from"], new_valid_to=new["valid_to"], new_num=new["value_num"],
                   new_text=new["value_text"], new_obj=new["object_entity"], old_valid_from=old["valid_from"],
                   old_valid_to=old["valid_to"], old_num=old["value_num"], old_text=old["value_text"],
                   old_obj=old["object_entity"], old_status=old["status"], new_doc=new["document_id"], old_doc=old["document_id"])
        if d.action == "ignore":
            continue
        if d.action in ("corroborate", "merge"):
            new_for_old = _add_origin(conn, old["id"], new_origin, new_rel)
            old_origin, old_rel = _claim_origin(conn, old["id"])
            _add_origin(conn, claim_id, old_origin, old_rel)
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE claims SET status = CASE WHEN status = 'contested' THEN status ELSE 'corroborated' END,
                                         attrs = attrs || %s WHERE id = ANY(%s)""",
                    (Jsonb({"reconcile": d.action, "with": old["id"] if True else None}), [old["id"], claim_id]),
                )
                cur.execute("UPDATE claims SET attrs = attrs || %s WHERE id = %s",
                            (Jsonb({"corroborates": old["id"], "new_origin": new_for_old}), claim_id))
            outcome = d.action
            stats.corroborated += d.action == "corroborate"
            stats.merged += d.action == "merge"
        elif d.action == "contest":
            with conn.cursor() as cur:
                cur.execute("UPDATE claims SET status = 'contested' WHERE id = ANY(%s)", ([old["id"], claim_id],))
                cur.execute(
                    """INSERT INTO conflicts(claim_a, claim_b, kind, region_id, geo_key)
                       VALUES (%s, %s, %s, (SELECT region_id FROM entities WHERE id = %s), %s)
                       ON CONFLICT (claim_a, claim_b) DO NOTHING""",
                    (old["id"], claim_id, "numeric_disagreement" if new["value_num"] is not None else "contradiction",
                     new["subject_entity"], new["geo_key"]),
                )
                stats.conflicts_opened += cur.rowcount
            outcome = "contest"
            stats.contested += 1
        elif d.action == "supersede":
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE claims SET status = 'superseded', superseded_by = %s,
                                         valid_to = COALESCE(valid_to, %s) WHERE id = %s""",
                    (claim_id, (new["valid_from"] - timedelta(days=1)) if new["valid_from"] else None, old["id"]),
                )
            outcome = "supersede"
            stats.superseded += 1
        elif d.action == "supersede_new":   # the new claim is the older period: close it, the existing claim stays current
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE claims SET status = 'superseded', superseded_by = %s,
                                         valid_to = COALESCE(valid_to, %s) WHERE id = %s""",
                    (old["id"], (old["valid_from"] - timedelta(days=1)) if old["valid_from"] else None, claim_id),
                )
            outcome = "supersede"
            stats.superseded += 1
    with conn.cursor() as cur:
        cur.execute("UPDATE claims SET reconciled_at = now() WHERE id = %s", (claim_id,))
    stats.processed += 1
    stats.untouched += outcome == "untouched"
    stats.by_predicate[new["predicate"]] = stats.by_predicate.get(new["predicate"], 0) + 1
    return outcome


def run_reconcile(conn: psycopg.Connection, *, region_id: str | None = None, include_structured: bool = False,
                  limit: int = 10000) -> dict:
    """Reconcile every unreconciled claim (oldest first). Structured indicator claims are already revision-
    managed by the ingest runner and are skipped unless asked for."""
    with job_run(conn, "reconcile.run", region_id) as run:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id FROM claims c
                   WHERE c.reconciled_at IS NULL
                     AND (%s OR c.extractor_version NOT LIKE 'structured@%%')
                     AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM entities e WHERE e.id = c.subject_entity AND e.region_id = %s))
                   ORDER BY c.id LIMIT %s""",
                (include_structured, region_id, region_id, limit),
            )
            ids = [r["id"] for r in cur.fetchall()]
        stats = ReconcileStats()
        for cid in ids:
            reconcile_claim(conn, cid, stats)
            if stats.processed % 500 == 0:
                conn.commit()
        conn.commit()
        run.stats.update({k: v for k, v in stats.__dict__.items() if k != "by_predicate"})
        return dict(run.stats)


def set_half_lives(conn: psycopg.Connection) -> int:
    """Fill half_life_days for claims that lack one, from the predicate class table."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT predicate FROM claims WHERE half_life_days IS NULL")
        preds = [r["predicate"] for r in cur.fetchall()]
        n = 0
        for p in preds:
            cur.execute("UPDATE claims SET half_life_days = %s WHERE predicate = %s AND half_life_days IS NULL",
                        (half_life_for(p), p))
            n += cur.rowcount
    return n


def recheck_conflicts(conn: psycopg.Connection, *, region_id: str | None = None) -> dict:
    """Re-run the decision table on open conflicts (after a rules change). Pairs that no longer contest are
    resolved: both_valid for distinct/agreeing facts (claims return to asserted), or a supersession."""
    from argusvue.reconcile.rules import decide

    with conn.cursor() as cur:
        cur.execute("""SELECT k.id, k.claim_a, k.claim_b FROM conflicts k WHERE k.resolved_at IS NULL
                       AND (%s::text IS NULL OR k.region_id = %s) ORDER BY k.id""", (region_id, region_id))
        open_ = cur.fetchall()
    out = {"checked": 0, "both_valid": 0, "superseded": 0, "still_open": 0}
    for k in open_:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM claims WHERE id = %s", (k["claim_a"],))
            a = cur.fetchone()
            cur.execute("SELECT * FROM claims WHERE id = %s", (k["claim_b"],))
            b = cur.fetchone()
        d = decide(b["predicate"], new_valid_from=b["valid_from"], new_valid_to=b["valid_to"], new_num=b["value_num"], new_text=b["value_text"],
                   new_obj=b["object_entity"], old_valid_from=a["valid_from"], old_valid_to=a["valid_to"], old_num=a["value_num"],
                   old_text=a["value_text"], old_obj=a["object_entity"], old_status="asserted", new_doc=b["document_id"], old_doc=a["document_id"])
        out["checked"] += 1
        with conn.cursor() as cur:
            if d.action == "contest":
                out["still_open"] += 1
                continue
            if d.action in ("supersede", "supersede_new"):
                loser, winner = (a, b) if d.action == "supersede" else (b, a)
                cur.execute("""UPDATE claims SET status = 'superseded', superseded_by = %s, valid_to = COALESCE(valid_to, %s) WHERE id = %s""",
                            (winner["id"], (winner["valid_from"] - timedelta(days=1)) if winner["valid_from"] else None, loser["id"]))
                cur.execute("UPDATE claims SET status = 'asserted' WHERE id = %s AND status = 'contested'", (winner["id"],))
                resolution = "keep_b" if winner["id"] == b["id"] else "keep_a"
                out["superseded"] += 1
            else:
                cur.execute("UPDATE claims SET status = 'asserted' WHERE id = ANY(%s) AND status = 'contested'", ([a["id"], b["id"]],))
                resolution = "both_valid"
                out["both_valid"] += 1
            cur.execute("UPDATE conflicts SET resolved_at = now(), resolution = %s, notes = %s WHERE id = %s",
                        (resolution, f"recheck: {d.reason}", k["id"]))
    conn.commit()
    return out

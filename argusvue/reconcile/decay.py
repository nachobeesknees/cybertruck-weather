"""Confidence decay (SPEC §6): effective_confidence = confidence * 0.5 ** (age_days / half_life_days).
Nothing is stored; decay is computed at read time from observed_at and half_life_days."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

from argusvue.reconcile.rules import effective_confidence

RE_VERIFY_THRESHOLD = 0.4


def claim_effective_confidence(claim: dict, as_of: datetime | None = None) -> float:
    as_of = as_of or datetime.now(UTC)
    age_days = (as_of - claim["observed_at"]).total_seconds() / 86400.0
    return effective_confidence(float(claim["confidence"]), age_days, claim.get("half_life_days"))


def stale_claims(conn: psycopg.Connection, *, threshold: float = RE_VERIFY_THRESHOLD, load_bearing_only: bool = True,
                 limit: int = 200) -> list[dict]:
    """Claims whose effective confidence fell below the threshold. With load_bearing_only, only claims cited
    by an approved thesis version (phase 4 tables) are returned; before those tables exist, all claims."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('thesis_claims') IS NOT NULL AS present")
        has_theses = cur.fetchone()["present"]
        lb = ""
        if load_bearing_only and has_theses:
            lb = """AND EXISTS (SELECT 1 FROM thesis_claims tc JOIN theses t ON t.id = tc.thesis_id
                              WHERE tc.claim_id = c.id AND t.human_status = 'approved')"""
        cur.execute(
            f"""SELECT c.id, c.predicate, c.geo_key, c.confidence, c.observed_at, c.half_life_days, c.status,
                       claim_effective_confidence(c.confidence, c.observed_at, c.half_life_days) AS effective
                FROM claims c
                WHERE c.status NOT IN ('superseded', 'retracted')
                  AND claim_effective_confidence(c.confidence, c.observed_at, c.half_life_days) < %s {lb}
                ORDER BY effective ASC LIMIT %s""",
            (threshold, limit),
        )
        return cur.fetchall()

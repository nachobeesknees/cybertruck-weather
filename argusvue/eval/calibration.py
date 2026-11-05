"""Falsifier calibration (SPEC §10): outcomes log and Brier score once enough have resolved."""

from __future__ import annotations

import psycopg

MIN_RESOLVED = 10

_SQL = """
SELECT f.id, f.action, f.outcome, tv.confidence
FROM falsifiers f
JOIN theses t ON t.id = f.thesis_id
JOIN thesis_versions tv ON tv.thesis_id = f.thesis_id AND tv.version = f.version
WHERE (%s::text IS NULL OR t.region_id = %s)
"""


def compute(conn: psycopg.Connection, region_id: str | None = None, *, min_resolved: int = MIN_RESOLVED, **_opts) -> dict:
    with conn.cursor() as cur:
        cur.execute(_SQL, (region_id, region_id))
        rows = cur.fetchall()
    by_action: dict[str, dict[str, int]] = {a: {"triggered": 0, "expired": 0, "open": 0} for a in ("downgrade", "retire", "review")}
    sq = []
    for r in rows:
        by_action[r["action"]][r["outcome"] or "open"] += 1
        if r["outcome"]:
            p = float(r["confidence"])
            y = 1.0 if r["outcome"] == "expired" else 0.0
            sq.append((p - y) ** 2)
    n = len(sq)
    return {"by_action": by_action, "falsifiers": len(rows), "resolved": n,
            "brier": round(sum(sq) / n, 4) if n >= min_resolved else None, "n": n, "min_resolved": min_resolved}

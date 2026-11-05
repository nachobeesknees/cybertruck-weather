"""Staleness (SPEC §10): age and decay of the claims behind approved theses."""

from __future__ import annotations

import statistics

import psycopg

LOW_CONF = 0.4

_APPROVED = """
SELECT DISTINCT ON (c.id) c.id, t.region_id AS region,
       EXTRACT(EPOCH FROM (now() - c.observed_at)) / 86400.0 AS age_days, c.half_life_days,
       claim_effective_confidence(c.confidence, c.observed_at, c.half_life_days) AS eff
FROM thesis_claims tc
JOIN theses t ON t.id = tc.thesis_id AND t.human_status = 'approved' AND tc.version = t.approved_version
JOIN claims c ON c.id = tc.claim_id
WHERE (%s::text IS NULL OR t.region_id = %s)
ORDER BY c.id
"""

_ALL = """
SELECT c.id, coalesce(s.region_id, 'national') AS region,
       EXTRACT(EPOCH FROM (now() - c.observed_at)) / 86400.0 AS age_days, c.half_life_days,
       claim_effective_confidence(c.confidence, c.observed_at, c.half_life_days) AS eff
FROM claims c JOIN documents d ON d.id = c.document_id LEFT JOIN sources s ON s.id = d.source_id
WHERE c.extractor_version LIKE 'extract@%%' AND (%s::text IS NULL OR s.region_id = %s)
"""


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"claims": 0, "median_age_days": None, "pct_past_half_life": None, "pct_effective_below_0_4": None}
    past = sum(1 for r in rows if r["half_life_days"] and float(r["age_days"]) > r["half_life_days"])
    low = sum(1 for r in rows if float(r["eff"]) < LOW_CONF)
    return {"claims": n, "median_age_days": round(statistics.median(float(r["age_days"]) for r in rows), 1),
            "pct_past_half_life": round(100.0 * past / n, 1), "pct_effective_below_0_4": round(100.0 * low / n, 1)}


def compute(conn: psycopg.Connection, region_id: str | None = None, **_opts) -> dict:
    with conn.cursor() as cur:
        cur.execute(_APPROVED, (region_id, region_id))
        approved = cur.fetchall()
        out: dict = {"approved_theses_claims": _stats(approved)}
        if not approved:
            cur.execute(_ALL, (region_id, region_id))
            allc = cur.fetchall()
            out["note"] = "no approved theses cite claims yet; showing all extract@ claims instead"
            out["all_extracted_claims"] = _stats(allc)
            out["all_extracted_claims_by_region"] = {
                rid: _stats([r for r in allc if r["region"] == rid]) for rid in sorted({r["region"] for r in allc})}
    return out

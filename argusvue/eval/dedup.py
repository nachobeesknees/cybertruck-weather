"""Dedup funnel (SPEC §10): raw docs -> unique docs -> extracted docs -> claims; re-extraction rate."""

from __future__ import annotations

import psycopg

_SQL = """
WITH docs AS (
    SELECT d.id, coalesce(s.region_id, 'national') AS region, d.meta
    FROM documents d LEFT JOIN sources s ON s.id = d.source_id
),
ext AS (
    SELECT document_id, count(*) AS n, bool_or(status = 'done') AS done
    FROM document_extractions GROUP BY document_id
),
cl AS (
    SELECT document_id, count(*) AS n FROM claims WHERE extractor_version LIKE 'extract@%%' GROUP BY document_id
)
SELECT docs.region,
       count(*) AS raw_documents,
       count(*) FILTER (WHERE docs.meta ? 'last_seen_at') AS refetched_identical,
       count(*) FILTER (WHERE (docs.meta->>'near_dup_of') IS NOT NULL) AS near_duplicates,
       count(*) FILTER (WHERE ext.done) AS extracted_documents,
       count(*) FILTER (WHERE ext.n > 1) AS re_extracted_documents,
       coalesce(sum(cl.n), 0)::int AS claims
FROM docs LEFT JOIN ext ON ext.document_id = docs.id LEFT JOIN cl ON cl.document_id = docs.id
WHERE (%s::text IS NULL OR docs.region = %s)
GROUP BY docs.region ORDER BY docs.region
"""


def _row(r: dict) -> dict:
    raw, near, ext = r["raw_documents"], r["near_duplicates"], r["extracted_documents"]
    return {
        "raw_documents": raw,
        "exact_unique_documents": raw,  # exact duplicates are never inserted
        "refetched_identical": r["refetched_identical"],
        "near_duplicates": near,
        "unique_documents": raw - near,
        "extracted_documents": ext,
        "claims": r["claims"],
        "claims_per_extracted_document": round(r["claims"] / ext, 2) if ext else None,
        "re_extracted_documents": r["re_extracted_documents"],
        "re_extraction_rate": round(r["re_extracted_documents"] / ext, 3) if ext else None,
    }


def compute(conn: psycopg.Connection, region_id: str | None = None, **_opts) -> dict:
    with conn.cursor() as cur:
        cur.execute(_SQL, (region_id, region_id))
        rows = cur.fetchall()
    regions = {r["region"]: _row(r) for r in rows}
    total = {k: sum(r[k] for r in rows) for k in ("raw_documents", "refetched_identical", "near_duplicates",
                                                  "extracted_documents", "re_extracted_documents", "claims")}
    return {"regions": regions, "overall": _row(total)}

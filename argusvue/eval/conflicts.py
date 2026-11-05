"""Conflict flow (SPEC §10): opened / resolved counts and time-to-resolve."""

from __future__ import annotations

import psycopg

_SQL = """
SELECT count(*) AS opened_all,
       count(*) FILTER (WHERE opened_at > now() - interval '7 days') AS opened_7d,
       count(*) FILTER (WHERE opened_at > now() - interval '30 days') AS opened_30d,
       count(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolved_all,
       count(*) FILTER (WHERE resolved_at > now() - interval '7 days') AS resolved_7d,
       count(*) FILTER (WHERE resolved_at > now() - interval '30 days') AS resolved_30d,
       count(*) FILTER (WHERE resolved_at IS NULL) AS open,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (resolved_at - opened_at)) / 3600.0)
           FILTER (WHERE resolved_at IS NOT NULL) AS median_ttr_h,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (resolved_at - opened_at)) / 3600.0)
           FILTER (WHERE resolved_at IS NOT NULL) AS p90_ttr_h,
       max(EXTRACT(EPOCH FROM (now() - opened_at)) / 86400.0) FILTER (WHERE resolved_at IS NULL) AS oldest_open_days
FROM conflicts WHERE (%s::text IS NULL OR region_id = %s)
"""


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def compute(conn: psycopg.Connection, region_id: str | None = None, **_opts) -> dict:
    with conn.cursor() as cur:
        cur.execute(_SQL, (region_id, region_id))
        r = cur.fetchone()
        cur.execute("SELECT kind, count(*) AS n FROM conflicts WHERE resolved_at IS NULL AND (%s::text IS NULL OR region_id = %s) "
                    "GROUP BY kind ORDER BY kind", (region_id, region_id))
        by_kind = {k["kind"]: k["n"] for k in cur.fetchall()}
    return {
        "opened": {"all": r["opened_all"], "7d": r["opened_7d"], "30d": r["opened_30d"]},
        "resolved": {"all": r["resolved_all"], "7d": r["resolved_7d"], "30d": r["resolved_30d"]},
        "median_time_to_resolve_hours": _r(r["median_ttr_h"]),
        "p90_time_to_resolve_hours": _r(r["p90_ttr_h"]),
        "open": r["open"], "open_by_kind": by_kind, "oldest_open_age_days": _r(r["oldest_open_days"]),
    }

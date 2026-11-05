"""Spend and job activity per region-day (SPEC §10), by LLM role/model and by job ('adapter')."""

from __future__ import annotations

import psycopg

from argusvue.settings import get_settings

_LLM = """
SELECT (created_at AT TIME ZONE %(tz)s)::date AS day, coalesce(region_id, '-') AS region, role, model,
       count(*) AS calls, coalesce(sum(input_tokens + cache_read_tokens + cache_write_tokens), 0)::bigint AS input_tokens,
       coalesce(sum(output_tokens), 0)::bigint AS output_tokens, coalesce(sum(cost_usd), 0) AS usd
FROM agent_runs
WHERE (created_at AT TIME ZONE %(tz)s)::date > (now() AT TIME ZONE %(tz)s)::date - %(days)s
  AND (%(region)s::text IS NULL OR region_id = %(region)s)
GROUP BY 1, 2, 3, 4 ORDER BY 1 DESC, 2, 3, 4
"""

_JOBS = """
SELECT (started_at AT TIME ZONE %(tz)s)::date AS day, coalesce(region_id, '-') AS region, job AS adapter,
       count(*) AS runs, count(*) FILTER (WHERE ok IS FALSE) AS failures
FROM job_runs
WHERE (started_at AT TIME ZONE %(tz)s)::date > (now() AT TIME ZONE %(tz)s)::date - %(days)s
  AND (%(region)s::text IS NULL OR region_id = %(region)s)
GROUP BY 1, 2, 3 ORDER BY 1 DESC, 2, 3
"""


def compute(conn: psycopg.Connection, region_id: str | None = None, *, days: int = 7, **_opts) -> dict:
    st = get_settings()
    p = {"tz": st.timezone, "days": days, "region": region_id}
    with conn.cursor() as cur:
        cur.execute(_LLM, p)
        llm = [{"day": r["day"].isoformat(), "region": r["region"], "role": r["role"], "model": r["model"], "calls": r["calls"],
                "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"], "usd": round(float(r["usd"]), 4)}
               for r in cur.fetchall()]
        cur.execute(_JOBS, p)
        jobs = [{"day": r["day"].isoformat(), "region": r["region"], "adapter": r["adapter"], "runs": r["runs"],
                 "failures": r["failures"]} for r in cur.fetchall()]
        cur.execute("SELECT (now() AT TIME ZONE %s)::date AS today, "
                    "(SELECT coalesce(sum(cost_usd), 0) FROM agent_runs "
                    " WHERE (created_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date) AS spent",
                    (st.timezone, st.timezone, st.timezone))
        t = cur.fetchone()
    cap = st.daily_spend_cap_usd
    spent = float(t["spent"])
    totals = {"calls": sum(r["calls"] for r in llm), "input_tokens": sum(r["input_tokens"] for r in llm),
              "output_tokens": sum(r["output_tokens"] for r in llm), "usd": round(sum(r["usd"] for r in llm), 4),
              "job_runs": sum(r["runs"] for r in jobs), "job_failures": sum(r["failures"] for r in jobs)}
    return {"days": days, "timezone": st.timezone, "llm": llm, "jobs": jobs, "totals": totals,
            "today": {"day": t["today"].isoformat(), "usd": round(spent, 4), "cap_usd": cap,
                      "pct_of_cap": round(100.0 * spent / cap, 1) if cap else None},
            "note": "today's cap usage is global (all regions)" if region_id else None}

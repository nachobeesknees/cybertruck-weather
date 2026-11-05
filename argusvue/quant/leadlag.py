"""Lead-lag tests for causal edges that name a cause and effect indicator."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import psycopg
from psycopg.types.json import Jsonb


def _month_index(d) -> int:
    return d.year * 12 + d.month - 1


def _load(conn, indicator_id: str, geo_key: str) -> dict[int, float]:
    with conn.cursor() as cur:
        cur.execute("SELECT period, value FROM indicator_latest WHERE indicator_id=%s AND geo_key=%s", (indicator_id, geo_key))
        return {_month_index(r["period"]): float(r["value"]) for r in cur.fetchall()}


def _diffs(series: dict[int, float], lo: int, hi: int) -> np.ndarray:
    """First differences (of logs when all values > 0) on the month grid lo..hi; NaN where a month is missing."""
    use_log = all(v > 0 for v in series.values())
    arr = np.full(hi - lo + 1, np.nan)
    for m, v in series.items():
        if lo <= m <= hi:
            arr[m - lo] = math.log(v) if use_log else v
    return np.diff(arr)


def _corr(x: np.ndarray, y: np.ndarray) -> tuple[float | None, int]:
    ok = ~(np.isnan(x) | np.isnan(y))
    n = int(ok.sum())
    if n < 4:
        return None, n
    xs, ys = x[ok], y[ok]
    if xs.std() == 0 or ys.std() == 0:
        return None, n
    return float(np.corrcoef(xs, ys)[0, 1]), n


def _pvalue(r: float, n: int) -> float | None:
    if n <= 3:
        return None
    r = max(min(r, 0.999999), -0.999999)
    z = math.atanh(r) * math.sqrt(n - 3)
    return math.erfc(abs(z) / math.sqrt(2))


def lag_scan(cause: dict[int, float], effect: dict[int, float], max_lag: int, hyp_lag: int) -> dict:
    lo, hi = max(min(cause), min(effect)), min(max(cause), max(effect))
    c, e = _diffs(cause, lo, hi), _diffs(effect, lo, hi)
    best = None
    by_lag: dict[int, tuple[float, int]] = {}
    for lag in range(max_lag + 1):
        # cause at t-lag vs effect at t
        r, n = _corr(c[: len(c) - lag] if lag else c, e[lag:])
        if r is None:
            continue
        by_lag[lag] = (r, n)
        if best is None or abs(r) > abs(by_lag[best][0]):
            best = lag
    out: dict = {}
    if best is None:
        return {"reason": "no_variance"}
    r, n = by_lag[best]
    out.update(best_lag=best, corr_best=round(r, 4), p_value_best=_pvalue(r, n), n_best=n)
    h = by_lag.get(hyp_lag)
    out["corr_at_hypothesised_lag"] = round(h[0], 4) if h else None
    return out


def test_causal_edges(conn: psycopg.Connection, region_id: str | None = None, *, min_points: int = 36,
                      max_lag: int = 12) -> dict:
    stats = {"edges": 0, "tested": 0, "insufficient": 0, "results": []}
    now = datetime.now(UTC).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.id, e.cause_indicator, e.effect_indicator, e.geo_key, e.lag_months, e.thesis_id, e.version,
                      ci.frequency AS cf, ei.frequency AS ef
               FROM causal_edges e JOIN theses t ON t.id = e.thesis_id
               JOIN indicators ci ON ci.id = e.cause_indicator JOIN indicators ei ON ei.id = e.effect_indicator
               WHERE t.active AND e.geo_key IS NOT NULL AND (%(r)s::text IS NULL OR t.region_id = %(r)s)
               ORDER BY e.id""",
            {"r": region_id},
        )
        edges = cur.fetchall()
        for e in edges:
            stats["edges"] += 1
            if e["cf"] != "monthly" or e["ef"] != "monthly":
                res = {"n": 0, "reason": "not_monthly"}
            else:
                cause = _load(conn, e["cause_indicator"], e["geo_key"])
                effect = _load(conn, e["effect_indicator"], e["geo_key"])
                n = len(set(cause) & set(effect))
                res = {"n": n, "reason": "insufficient_overlap"} if n < min_points else {"n": n}
                if n >= min_points:
                    res.update(lag_scan(cause, effect, max_lag, e["lag_months"]))
                    res["method"] = "pearson_diff_log"
            res["computed_at"] = now
            tested = "best_lag" in res
            res_row = {"edge_id": e["id"], **{k: v for k, v in res.items() if k != "computed_at"}}
            cur.execute("UPDATE causal_edges SET test_result = %s, status = %s WHERE id = %s",
                        (Jsonb(res), "tested" if tested else "hypothesis", e["id"]))
            stats["tested" if tested else "insufficient"] += 1
            stats["results"].append(res_row)
    conn.commit()
    return stats


test_causal_edges.__test__ = False  # not a pytest test, despite the name

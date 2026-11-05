"""Indicator anomalies: z-score vs trailing 36 points, YoY and period-over-period change.

History is kept: every run inserts a new `computed_at` batch and never edits earlier rows."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

import psycopg

WINDOW = 36
MIN_PRIOR = 12


def year_ago(period: date, frequency: str) -> date:
    """The period date one year earlier, by frequency (matched on date, not index)."""
    if frequency == "weekly":
        return period - timedelta(weeks=52)
    if frequency == "daily":
        return period - timedelta(days=365)
    try:  # monthly (12 periods), quarterly (4), annual (1) all land on the same calendar day a year back
        return period.replace(year=period.year - 1)
    except ValueError:  # Feb 29
        return period.replace(year=period.year - 1, day=28)


def _pct(cur: float, prev: float | None) -> float | None:
    if prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100.0


def series_stats(points: list[tuple[date, float]], frequency: str, last_n: int) -> list[dict]:
    """points sorted by period. Stats for the most recent `last_n` periods."""
    by_date = dict(points)
    out = []
    for i in range(len(points) - 1, max(len(points) - last_n, 0) - 1, -1):
        period, value = points[i]
        prior = [v for _, v in points[max(0, i - WINDOW):i]]
        z = None
        if len(prior) >= MIN_PRIOR:
            sd = statistics.stdev(prior)
            if sd > 0:
                z = (value - statistics.fmean(prior)) / sd
        out.append({
            "period": period, "value": value, "zscore": z,
            "yoy_pct": _pct(value, by_date.get(year_ago(period, frequency))),
            "mom_pct": _pct(value, points[i - 1][1] if i > 0 else None),
        })
    return out


def compute_anomalies(conn: psycopg.Connection, region_id: str, *, last_n: int = 3) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.indicator_id, l.geo_key, l.period, l.value, i.frequency
               FROM indicator_latest l
               JOIN indicators i ON i.id = l.indicator_id
               JOIN geo_units g ON g.geo_key = l.geo_key
               WHERE g.region_id = %s OR l.geo_key = 'country:US'
               ORDER BY l.indicator_id, l.geo_key, l.period""",
            (region_id,),
        )
        rows = cur.fetchall()
        series: dict[tuple[str, str, str], list[tuple[date, float]]] = defaultdict(list)
        for r in rows:
            series[(r["indicator_id"], r["geo_key"], r["frequency"])].append((r["period"], float(r["value"])))
        cur.execute("SELECT now() AS t")
        computed_at = cur.fetchone()["t"]
        n_rows = n_z = n_big = 0
        for (ind, geo, freq), pts in series.items():
            for s in series_stats(pts, freq, last_n):
                cur.execute(
                    """INSERT INTO indicator_anomalies(indicator_id, geo_key, period, value, zscore, yoy_pct, mom_pct, computed_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (ind, geo, s["period"], s["value"], s["zscore"], s["yoy_pct"], s["mom_pct"], computed_at),
                )
                n_rows += 1
                if s["zscore"] is not None:
                    n_z += 1
                    n_big += abs(s["zscore"]) > 2
    conn.commit()
    return {"series": len(series), "anomaly_rows": n_rows, "with_zscore": n_z, "abs_z_gt_2": n_big,
            "computed_at": computed_at.isoformat()}


def latest_anomalies(conn: psycopg.Connection, region_id: str, *, min_abs_z: float = 2.0, limit: int = 50) -> list[dict]:
    """Biggest |z| moves in the region's latest computed batch (batches are per region run)."""
    with conn.cursor() as cur:
        cur.execute(
            """WITH b AS (
                   SELECT a.* FROM indicator_anomalies a
                   JOIN geo_units g ON g.geo_key = a.geo_key
                   WHERE (g.region_id = %(r)s OR a.geo_key = 'country:US')
                     AND a.computed_at = (SELECT max(a2.computed_at) FROM indicator_anomalies a2
                                          JOIN geo_units g2 ON g2.geo_key = a2.geo_key WHERE g2.region_id = %(r)s)
               )
               SELECT b.*, i.name, i.unit FROM b JOIN indicators i ON i.id = b.indicator_id
               WHERE b.zscore IS NOT NULL AND abs(b.zscore) >= %(z)s
               ORDER BY abs(b.zscore) DESC LIMIT %(n)s""",
            {"r": region_id, "z": min_abs_z, "n": limit},
        )
        return cur.fetchall()

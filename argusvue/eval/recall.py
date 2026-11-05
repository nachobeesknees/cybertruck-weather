"""Recall audit (SPEC §10): did we capture the notable events in eval/events.yaml, and how fast?"""

from __future__ import annotations

import statistics
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import yaml

EVENTS_PATH = Path(__file__).with_name("events.yaml")
WINDOW_BEFORE_DAYS = 2
WINDOW_AFTER_DAYS = 14
MIN_KEYWORDS = 2


def load_events(path: Path | None = None) -> list[dict]:
    data = yaml.safe_load((path or EVENTS_PATH).read_text(encoding="utf-8")) or {}
    out = []
    for e in data.get("events", []):
        d = e["date"]
        if isinstance(d, str):
            d = date.fromisoformat(d)
        out.append({**e, "date": d, "keywords": [str(k) for k in e.get("keywords", [])]})
    return out


def _hits(text: str | None, keywords: list[str]) -> int:
    t = (text or "").lower()
    return sum(1 for k in keywords if k.lower() in t)


def _norm_url(u: str | None) -> str:
    return (u or "").strip().lower().rstrip("/")


def _event_status(conn: psycopg.Connection, ev: dict) -> dict:
    start = datetime.combine(ev["date"] - timedelta(days=WINDOW_BEFORE_DAYS), datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(ev["date"] + timedelta(days=WINDOW_AFTER_DAYS + 1), datetime.min.time(), tzinfo=UTC)
    kws = ev["keywords"]
    ev_url = _norm_url(ev.get("url"))
    matches: list[tuple[datetime, str, str]] = []  # (fetched_at, kind, ref)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.id, d.url, d.title, d.meta->>'feed_summary' AS summary, d.fetched_at
               FROM documents d JOIN sources s ON s.id = d.source_id
               WHERE s.kind <> 'structured' AND (s.region_id = %s OR s.region_id IS NULL)
                 AND ((d.published_at >= %s AND d.published_at < %s) OR (d.fetched_at >= %s AND d.fetched_at < %s))""",
            (ev["region"], start, end, start, end),
        )
        for r in cur.fetchall():
            by_url = bool(ev_url) and _norm_url(r["url"]) == ev_url
            if by_url or _hits(f"{r['title'] or ''} {r['summary'] or ''}", kws) >= MIN_KEYWORDS:
                matches.append((r["fetched_at"], "document", str(r["id"])))
        cur.execute(
            """SELECT c.id, c.excerpt, d.fetched_at
               FROM claims c JOIN documents d ON d.id = c.document_id
               LEFT JOIN sources s ON s.id = d.source_id
               WHERE c.observed_at >= %s AND c.observed_at < %s
                 AND (s.region_id = %s OR s.region_id IS NULL)""",
            (start, end, ev["region"]),
        )
        for r in cur.fetchall():
            if _hits(r["excerpt"], kws) >= MIN_KEYWORDS:
                matches.append((r["fetched_at"], "claim", str(r["id"])))
    if not matches:
        return {"id": ev["id"], "region": ev["region"], "date": ev["date"].isoformat(), "title": ev["title"],
                "captured": False, "latency_days": None, "matches": 0, "first_match": None}
    matches.sort(key=lambda m: m[0])
    first = matches[0]
    latency = (first[0] - datetime.combine(ev["date"], datetime.min.time(), tzinfo=UTC)).total_seconds() / 86400.0
    return {"id": ev["id"], "region": ev["region"], "date": ev["date"].isoformat(), "title": ev["title"],
            "captured": True, "latency_days": round(latency, 2), "matches": len(matches),
            "first_match": f"{first[1]}:{first[2]}"}


def compute(conn: psycopg.Connection, region_id: str | None = None, *, events_path: Path | None = None, **_opts) -> dict:
    events = [e for e in load_events(events_path) if region_id in (None, e["region"])]
    statuses = [_event_status(conn, e) for e in events]
    regions: dict[str, dict] = {}
    for rid in sorted({s["region"] for s in statuses}):
        sts = [s for s in statuses if s["region"] == rid]
        lat = [s["latency_days"] for s in sts if s["captured"]]
        regions[rid] = {
            "events": len(sts), "captured": len(lat),
            "pct_captured": round(100.0 * len(lat) / len(sts), 1) if sts else None,
            "median_latency_days": round(statistics.median(lat), 2) if lat else None,
        }
    lat_all = [s["latency_days"] for s in statuses if s["captured"]]
    overall = {"events": len(statuses), "captured": len(lat_all),
               "pct_captured": round(100.0 * len(lat_all) / len(statuses), 1) if statuses else None,
               "median_latency_days": round(statistics.median(lat_all), 2) if lat_all else None}
    return {"regions": regions, "overall": overall, "events": statuses}

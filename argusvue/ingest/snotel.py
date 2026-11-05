"""USDA NRCS SNOTEL snow water equivalent via the AWDB REST API (end-of-month SWE per station)."""

from __future__ import annotations

import calendar
import re
import time
from collections.abc import Iterable
from datetime import date, timedelta

import httpx

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource
from argusvue.settings import USER_AGENT

# one daily record, tolerant of whitespace; value must be numeric (nulls/missing are skipped)
_REC = re.compile(r'\{\s*"date"\s*:\s*"(\d{4}-\d{2}-\d{2})"\s*,\s*"value"\s*:\s*(-?\d+(?:\.\d+)?)\s*\}')
MAX_GAP_DAYS = 3
REQUEST_DELAY_S = 1.0


class SnotelSource(StructuredSource):
    slug = "snotel"

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        base = cfg["base"].rstrip("/")
        element = cfg.get("element", "WTEQ")
        begin = cfg["begin_date"] if ctx.backfill else (ctx.today - timedelta(days=int(cfg["incremental_days"]))).isoformat()
        end = ctx.today.isoformat()
        specs: list[FetchSpec] = []
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as client:
            for i, st in enumerate(cfg["stations"]):
                if i:
                    time.sleep(REQUEST_DELAY_S)
                triplet = st["triplet"]
                r = client.get(f"{base}/stations", params={"stationTriplets": triplet})
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    raise RuntimeError(f"SNOTEL station metadata empty for {triplet}")
                meta = rows[0]
                time.sleep(REQUEST_DELAY_S)
                url = (f"{base}/data?stationTriplets={triplet}&elements={element}&duration=DAILY"
                       f"&beginDate={begin}&endDate={end}")
                specs.append(FetchSpec(
                    url=url, key=st["indicator"], suffix=".json", title=f"SNOTEL {meta.get('name', triplet)} SWE",
                    meta={"triplet": triplet, "indicator": st["indicator"], "lat": float(meta["latitude"]),
                          "lon": float(meta["longitude"]), "name": meta.get("name", "")},
                ))
        return specs

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        m = doc.meta
        geo = ctx.containing(m["lat"], m["lon"], "county")
        if geo is None:
            return  # station outside the gazetteer (e.g. Granite Creek may be outside the region)
        text = doc.text()
        last_by_month: dict[tuple[int, int], tuple[date, str, str, int, int]] = {}
        for mt in _REC.finditer(text):
            d = date.fromisoformat(mt.group(1))
            if float(mt.group(2)) < 0:
                continue
            key = (d.year, d.month)
            prev = last_by_month.get(key)
            if prev is None or d > prev[0]:
                last_by_month[key] = (d, mt.group(2), mt.group(0), mt.start(), mt.end())
        today = ctx.today
        for (y, mo), (d, val, rec, s, e) in sorted(last_by_month.items()):
            month_end = date(y, mo, calendar.monthrange(y, mo)[1])
            if today < month_end:
                continue  # current month is incomplete
            if (month_end - d).days > MAX_GAP_DAYS:
                continue
            yield Point(indicator_id=m["indicator"], geo_key=geo, period=date(y, mo, 1), value=float(val),
                        excerpt=rec, excerpt_start=s, excerpt_end=e, unit="inches",
                        attrs={"station": m["triplet"], "obs_date": d.isoformat()})

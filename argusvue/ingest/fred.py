"""FRED CSV mirror of BLS LAUS (unemployment) and CES (nonfarm employment) series.

Config `series` is a list of `{ids: [candidate ids in order], indicator, geo}`. `fetch_specs` picks, per entry, the
first candidate id that FRED serves (a streamed GET that is closed after the status line, so no body is read) and
records it in `FetchSpec.meta`. CSV: `observation_date,SERIES` then `1990-01-01,4.6`; `.` means missing.
Attribution: Federal Reserve Bank of St. Louis (FRED) and U.S. Bureau of Labor Statistics.
"""

from __future__ import annotations

import csv
import logging
import time
from collections.abc import Iterable
from datetime import date

import httpx

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource
from argusvue.settings import USER_AGENT

log = logging.getLogger(__name__)

DELAY_S = 1.5


def seasonal_flag(series_id: str) -> str:
    """FRED convention: trailing N = not seasonally adjusted; BLS SMU* series are NSA. Everything else SA."""
    return "NSA" if series_id.endswith("N") or series_id.startswith("SMU") else "SA"


class FredSource(StructuredSource):
    slug = "fred"

    def _exists(self, client: httpx.Client, url: str) -> bool:
        with client.stream("GET", url) as r:
            if r.status_code in (400, 404):
                return False
            r.raise_for_status()
            return True

    def fetch_specs(self, ctx: IngestContext, client: httpx.Client | None = None, delay_s: float = DELAY_S) -> list[FetchSpec]:
        base = ctx.source_config["base"]
        own = client is None
        client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True)
        specs: list[FetchSpec] = []
        try:
            for entry in ctx.source_config["series"]:
                geo = entry["geo"]
                if not ctx.in_region(geo):
                    continue
                chosen = None
                for sid in entry["ids"]:
                    time.sleep(delay_s)
                    if self._exists(client, f"{base}{sid}"):
                        chosen = sid
                        break
                    log.warning("FRED series %s not found; trying next candidate", sid)
                if chosen is None:
                    raise RuntimeError(f"none of the FRED candidates exist for {entry['indicator']} {geo}: {entry['ids']}")
                specs.append(FetchSpec(url=f"{base}{chosen}", key=f"{entry['indicator']}|{geo}", suffix=".csv",
                                       meta={"series_id": chosen, "indicator": entry["indicator"], "geo": geo,
                                             "candidates": list(entry["ids"])}))
        finally:
            if own:
                client.close()
        return specs

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        indicator, geo, sid = doc.meta.get("indicator"), doc.meta.get("geo"), doc.meta.get("series_id")
        if not (indicator and geo and sid):
            raise ValueError("FRED document is missing indicator/geo/series_id meta")
        if not ctx.in_region(geo):
            return
        seasonal = seasonal_flag(sid)
        header_seen = False
        for offset, line in doc.iter_lines():
            if not line.strip():
                continue
            row = next(csv.reader([line]))
            if not header_seen:
                if len(row) < 2 or row[0] != "observation_date" or row[1] != sid:
                    raise ValueError(f"unexpected FRED header for {sid}: {line!r}")
                header_seen = True
                continue
            raw = row[1].strip() if len(row) > 1 else ""
            if raw in ("", "."):
                continue
            try:
                period = date.fromisoformat(row[0])
                value = float(raw)
            except ValueError:
                continue
            yield Point(indicator_id=indicator, geo_key=geo, period=period, value=value,
                        excerpt=line, excerpt_start=offset, excerpt_end=offset + len(line),
                        attrs={"series_id": sid, "seasonal": seasonal})

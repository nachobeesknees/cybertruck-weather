"""NPS Visitor Use Statistics (IRMA stats web service): Grand Teton NP monthly recreation visits.

The host's robots.txt disallows crawlers; the documented project policy (DECISIONS 2026-11-05) allows this
single publisher-cadence API call. Exactly one request is made per run and nothing else to the host.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource

_OBJ = re.compile(r"\{[^{}]*\}")
INDICATOR = "grte_rec_visits"


class NpsStatsSource(StructuredSource):
    slug = "nps_stats"

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        end_year = ctx.today.year
        start_year = int(cfg["start_year"]) if ctx.backfill else end_year - 1
        url = (f"{cfg['base']}?unitCodes={cfg['unit_code']}&startMonth=1&startYear={start_year}"
               f"&endMonth=12&endYear={end_year}")
        return [FetchSpec(url=url, key=cfg["unit_code"], suffix=".json", headers={"Accept": "application/json"},
                          title=f"NPS visitation {cfg['unit_code']} {start_year}-{end_year}")]

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        geo = ctx.source_config["geo"]
        unit = ctx.source_config["unit_code"]
        text = doc.text()
        for mt in _OBJ.finditer(text):
            try:
                rec = json.loads(mt.group(0))
            except ValueError:
                continue
            v = rec.get("RecreationVisitors")
            if rec.get("UnitCode") != unit or isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            try:
                period = date(int(rec["Year"]), int(rec["Month"]), 1)
            except (KeyError, TypeError, ValueError):
                continue
            yield Point(indicator_id=INDICATOR, geo_key=geo, period=period, value=v, excerpt=mt.group(0),
                        excerpt_start=mt.start(), excerpt_end=mt.end(), attrs={"unit_code": unit})

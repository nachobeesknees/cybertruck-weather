"""Zillow Research public CSVs (ZHVI, ZORI, inventory, price cuts, days to pending).

Wide CSVs: `RegionID,SizeRank,RegionName,RegionType,StateName,[State,City,Metro,CountyName,]<YYYY-MM-DD...>`.
Metro rows map through config `metro_to_cbsa`; zip rows are 5-digit ZCTAs. Files are streamed line by line
and month cells are only parsed for rows whose region resolves in the gazetteer (zip files are 50-150 MB).
Backfill and incremental runs download the same files (each file holds the full history).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from datetime import date

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ZillowSource(StructuredSource):
    slug = "zillow"

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        base = cfg["base"].rstrip("/")
        specs: list[FetchSpec] = []
        for indicator, levels in cfg["files"].items():
            for level, path in levels.items():
                specs.append(FetchSpec(url=f"{base}/{path.lstrip('/')}", key=f"{indicator}_{level}", suffix=".csv",
                                       meta={"indicator": indicator, "level": level}))
        return specs

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        indicator = doc.meta.get("indicator")
        level = doc.meta.get("level")
        if not indicator or not level:
            indicator, _, level = doc.key.rpartition("_")
        metro_to_cbsa: dict[str, str] = ctx.source_config.get("metro_to_cbsa", {})

        header: list[str] | None = None
        month_cols: list[tuple[int, date]] = []
        name_idx = 2
        for offset, line in doc.iter_lines():
            if not line:
                continue
            if header is None:
                header = next(csv.reader([line]))
                name_idx = header.index("RegionName")
                for i, h in enumerate(header):
                    if _DATE_RE.match(h):
                        y, m, _ = h.split("-")
                        month_cols.append((i, date(int(y), int(m), 1)))
                continue

            # cheap region test before any full parse
            if level == "zip":
                name = line.split(",", name_idx + 1)[name_idx]
                geo_key = ctx.zcta(name) if name.isdigit() else None
                if geo_key is None:
                    continue
                row = next(csv.reader([line]))
            else:
                row = next(csv.reader([line]))
                code = metro_to_cbsa.get(row[name_idx])
                geo_key = ctx.cbsa(code) if code else None
                if geo_key is None:
                    continue

            end = offset + len(line)
            for i, period in month_cols:
                if i >= len(row):
                    break
                cell = row[i].strip()
                if not cell:
                    continue
                try:
                    value = float(cell)
                except ValueError:
                    continue
                yield Point(indicator_id=indicator, geo_key=geo_key, period=period, value=value,
                            excerpt=line, excerpt_start=offset, excerpt_end=end)

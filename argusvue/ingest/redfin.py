"""Redfin Data Center monthly housing market tracker (metro, county, zip, city).

Plain quoted CSVs with `NA` for nulls, mixing `FREQUENCY` values (`Monthly`, `Rolling 3 Months`, ...); only
the configured frequency (`Monthly`) is kept. The zip and city files are >1 GB, so parsing is one streaming
pass over `doc.iter_lines()`: a cheap regex pulls the frequency, region id and region name off the front of
each line, the region is resolved through the gazetteer, and only matching rows are CSV-parsed.
Backfill and incremental runs download the same files (each file holds the full history).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from datetime import date

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource

# "LAST UPDATED","FREQUENCY","PERIOD BEGIN","PERIOD END",REGION ID,"REGION TYPE","REGION NAME"
_HEAD_RE = re.compile(r'^"[^"]*","([^"]*)","[^"]*","[^"]*",("?)([^",]*)\2,"[^"]*","([^"]*)"')
_ZIP_PREFIX = re.compile(r"^zip(?: code)?:\s*", re.IGNORECASE)


class RedfinSource(StructuredSource):
    slug = "redfin"

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        base = cfg["base"].rstrip("/")
        return [FetchSpec(url=f"{base}/{path.lstrip('/')}", key=level, suffix=".csv", meta={"level": level})
                for level, path in cfg["files"].items()]

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        cfg = ctx.source_config
        level = doc.meta.get("level") or doc.key
        want_freq = cfg.get("frequency_filter", "Monthly")
        columns: dict[str, str] = cfg["columns"]

        header: list[str] | None = None
        col_idx: dict[str, int] = {}
        begin_idx = 2
        for offset, line in doc.iter_lines():
            if not line:
                continue
            if header is None:
                header = next(csv.reader([line]))
                begin_idx = header.index("PERIOD BEGIN")
                col_idx = {ind: header.index(col) for ind, col in columns.items()}
                continue

            m = _HEAD_RE.match(line)
            if m is not None:
                freq, region_id, region_name = m.group(1), m.group(3), m.group(4)
                row: list[str] | None = None
            else:  # unexpected quoting: fall back to a full parse of this line
                row = next(csv.reader([line]))
                freq, region_id, region_name = row[1], row[4], row[6]
            if freq != want_freq:
                continue
            geo_key = self._geo_key(level, region_id, region_name, ctx)
            if geo_key is None:
                continue

            if row is None:
                row = next(csv.reader([line]))
            try:
                y, mo, _ = row[begin_idx].split("-")
                period = date(int(y), int(mo), 1)
            except ValueError:
                continue
            end = offset + len(line)
            for ind, i in col_idx.items():
                cell = row[i].strip() if i < len(row) else ""
                if not cell or cell == "NA":
                    continue
                try:
                    value = float(cell)
                except ValueError:
                    continue
                yield Point(indicator_id=ind, geo_key=geo_key, period=period, value=value,
                            excerpt=line, excerpt_start=offset, excerpt_end=end)

    @staticmethod
    def _geo_key(level: str, region_id: str, name: str, ctx: IngestContext) -> str | None:
        if level == "metros":
            return ctx.cbsa(region_id.strip())
        if level == "zips":
            z = _ZIP_PREFIX.sub("", name).strip()
            return ctx.zcta(z) if z.isdigit() else None
        place, sep, state = name.rpartition(", ")
        if not sep:
            return None
        if level == "counties":
            return ctx.county_by_name(place, state)
        if level == "cities":
            return ctx.place_by_name(place, state)
        return None

"""Freddie Mac Primary Mortgage Market Survey (PMMS) history: weekly 30-year fixed rate, national context.

CSV header: `date,pmms30,pmms30p,pmms15,...`; dates are `M/D/YYYY` week dates (Thursdays). Only `pmms30` is
ingested (indicator `mortgage_rate_30y`, geo `country:US`). The file always holds the full history, so
backfill and incremental runs fetch the same document (the runner dedups by content hash).
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import datetime

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource

INDICATOR = "mortgage_rate_30y"
GEO = "country:US"


class PmmsSource(StructuredSource):
    slug = "freddie_mac"

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        return [FetchSpec(url=ctx.source_config["url"], key="pmms_history", suffix=".csv")]

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        if not ctx.in_region(GEO):
            return
        col: int | None = None
        for offset, line in doc.iter_lines():
            if not line.strip():
                continue
            row = next(csv.reader([line]))
            if col is None:
                header = [h.strip().lower() for h in row]
                if "date" not in header or "pmms30" not in header:
                    raise ValueError(f"unexpected PMMS header: {line!r}")
                if header[0] != "date":
                    raise ValueError(f"PMMS date column is not first: {line!r}")
                col = header.index("pmms30")
                continue
            if col >= len(row) or not row[col].strip():
                continue
            try:
                period = datetime.strptime(row[0].strip(), "%m/%d/%Y").date()
                value = float(row[col])
            except ValueError:
                continue
            yield Point(indicator_id=INDICATOR, geo_key=GEO, period=period, value=value,
                        excerpt=line, excerpt_start=offset, excerpt_end=offset + len(line))

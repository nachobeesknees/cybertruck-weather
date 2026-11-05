"""Census Population Estimates Program: county totals and components of change (wide CSVs).

One file per vintage; each row is a geography with `POPESTIMATE{yr}`, `NETMIG{yr}`, ... columns for every
year of the vintage. Files are listed oldest to newest so the newest vintage is ingested last and becomes the
current belief for overlapping years (older values stay as revisions). One raw county line is the excerpt for
all years and columns of that row. Files are latin-1 but our rows are ASCII, so the default decoding is used
(offsets must agree with the runner's verification).
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import date

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource


class PepSource(StructuredSource):
    slug = "census_pep"

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        files = sorted(ctx.source_config["files"], key=lambda f: int(f["vintage"]))
        return [FetchSpec(url=f["url"], key=f"pep_vintage{f['vintage']}", suffix=".csv",
                          meta={"vintage": int(f["vintage"]), "years": list(f["years"])}) for f in files]

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        columns: dict[str, str] = ctx.source_config["columns"]
        vintage = doc.meta["vintage"]
        y0, y1 = doc.meta["years"]
        idx: dict[str, int] = {}
        wanted: list[tuple[str, int, int]] = []     # (indicator, year, column index)
        for offset, line in doc.iter_lines():
            if not line:
                continue
            if not idx:
                idx = {name: i for i, name in enumerate(next(csv.reader([line])))}
                for prefix, indicator in columns.items():
                    for year in range(y0, y1 + 1):
                        col = f"{prefix}{year}"
                        if col not in idx:
                            raise ValueError(f"PEP vintage {vintage}: expected column {col} missing from header")
                        wanted.append((indicator, year, idx[col]))
                continue
            row = next(csv.reader([line]))
            if row[idx["SUMLEV"]] != "050":
                continue
            geo = ctx.county(row[idx["STATE"]].zfill(2) + row[idx["COUNTY"]].zfill(3))
            if geo is None:
                continue
            end = offset + len(line)
            for indicator, year, i in wanted:
                cell = row[i].strip() if i < len(row) else ""
                if not cell:
                    continue
                try:
                    value = int(cell)
                except ValueError:
                    continue
                yield Point(indicator_id=indicator, geo_key=geo, period=date(year, 1, 1), value=value, excerpt=line,
                            excerpt_start=offset, excerpt_end=end, attrs={"reference": "July 1", "vintage": vintage})

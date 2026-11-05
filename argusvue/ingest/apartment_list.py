"""Apartment List rent estimates: one wide monthly CSV whose URL changes every month.

`fetch_specs` loads the research page, finds the newest `Apartment_List_Rent_Estimates_YYYY_MM.csv` link on the
CDN (the `..._Summary_...` file does not match the pattern) and fetches it. Wide columns:
`location_name,location_type,location_fips_code,population,state,county,metro,bed_size,2017_01,...`.
Metro -> cbsa, County -> county (fips zero-padded to 5), City -> place (fips zero-padded to 7). Each file holds the
full history, so backfill and incremental fetch the same document.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from datetime import date

import httpx

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource
from argusvue.settings import USER_AGENT

_BED = {"overall": "aptlist_rent_overall", "1br": "aptlist_rent_1br", "2br": "aptlist_rent_2br"}
_MONTH_COL = re.compile(r"^(\d{4})_(\d{2})$")


def find_csv_url(html: str, cdn_host: str, file_pattern: str) -> tuple[str, str]:
    """Newest matching CSV link in the page markup -> (https url, 'YYYY_MM'). Raises if none is found."""
    rx = re.compile(r"(?:https?:)?//" + re.escape(cdn_host) + r"/[^\"'\s\\<>]*?/(" + file_pattern + r")")
    best: tuple[str, str, str] | None = None
    for m in rx.finditer(html):
        name = m.group(1)
        ym = re.search(r"(\d{4}_\d{2})\.csv$", name)
        if not ym:
            continue
        url = m.group(0)
        if url.startswith("//"):
            url = "https:" + url
        if best is None or ym.group(1) > best[0]:
            best = (ym.group(1), url, name)
    if best is None:
        raise RuntimeError("no Apartment List rent-estimates CSV link found on the page")
    return best[1], best[0]


class ApartmentListSource(StructuredSource):
    slug = "apartment_list"

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True) as client:
            r = client.get(cfg["page"])
            r.raise_for_status()
        url, ym = find_csv_url(r.text, cfg["cdn_host"], cfg["file_pattern"])
        return [FetchSpec(url=url, key="rent_estimates", suffix=".csv", meta={"page": cfg["page"], "file_month": ym})]

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        header: list[str] | None = None
        month_cols: list[tuple[int, date]] = []
        for offset, line in doc.iter_lines():
            if not line.strip():
                continue
            row = next(csv.reader([line]))
            if header is None:
                header = row
                for i, h in enumerate(header):
                    m = _MONTH_COL.match(h)
                    if m:
                        month_cols.append((i, date(int(m.group(1)), int(m.group(2)), 1)))
                idx = {h: i for i, h in enumerate(header)}
                for need in ("location_type", "location_fips_code", "bed_size"):
                    if need not in idx:
                        raise ValueError(f"unexpected Apartment List header, missing {need}: {line[:200]!r}")
                continue
            ind = _BED.get(row[idx["bed_size"]].strip().lower())
            if ind is None:
                continue
            ltype, fips = row[idx["location_type"]].strip().lower(), row[idx["location_fips_code"]].strip()
            if not fips.isdigit():
                continue
            if ltype == "metro":
                geo = ctx.cbsa(fips)
            elif ltype == "county":
                geo = ctx.county(fips.zfill(5))
            elif ltype == "city":
                geo = ctx.place(fips.zfill(7))
            else:
                continue
            if geo is None or not ctx.in_region(geo):
                continue
            end = offset + len(line)
            for i, period in month_cols:
                if i >= len(row) or not row[i].strip():
                    continue
                try:
                    value = float(row[i])
                except ValueError:
                    continue
                yield Point(indicator_id=ind, geo_key=geo, period=period, value=value,
                            excerpt=line, excerpt_start=offset, excerpt_end=end)

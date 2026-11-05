"""BLS QCEW open-data API: one CSV per (year, quarter, county).

Each CSV holds every ownership/industry cut for the county-quarter. We keep the county total (own 0,
industry 10, agglvl 70) and the configured private NAICS sectors (own 5, agglvl 74). One line yields
three monthly employment points plus (total row only) quarterly average weekly wage and establishments,
so the excerpt is the raw CSV line. Rows with disclosure_code 'N' (suppressed) are skipped.
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

PROBE_BACK_QUARTERS = 4
REQUEST_DELAY_S = 1.0


def _quarters_back(today: date) -> tuple[int, int]:
    """The calendar quarter before the one containing `today` (the newest that could be published)."""
    q = (today.month - 1) // 3 + 1
    return (today.year, q - 1) if q > 1 else (today.year - 1, 4)


def _prev(year: int, q: int) -> tuple[int, int]:
    return (year, q - 1) if q > 1 else (year - 1, 4)


class QcewSource(StructuredSource):
    slug = "bls_qcew"

    def _url(self, ctx: IngestContext, year: int, q: int, fips: str) -> str:
        return f"{ctx.source_config['base'].rstrip('/')}/{year}/{q}/area/{fips}.csv"

    def _latest_available(self, ctx: IngestContext, fips: str) -> tuple[int, int]:
        year, q = _quarters_back(ctx.today)
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as client:
            for _ in range(PROBE_BACK_QUARTERS):
                r = client.head(self._url(ctx, year, q, fips))
                time.sleep(REQUEST_DELAY_S)
                if r.status_code == 200:
                    return year, q
                if r.status_code != 404:
                    r.raise_for_status()
                year, q = _prev(year, q)
        raise RuntimeError(f"no QCEW quarter found within {PROBE_BACK_QUARTERS} quarters of {ctx.today}")

    def _earliest_year(self, ctx: IngestContext, fips: str, newest_year: int) -> int:
        """The open-data API does not serve every year back to `first_year` (it starts around 2014), so
        find the first year whose Q1 file exists, probing upward from the configured first year."""
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as client:
            for year in range(int(ctx.source_config["first_year"]), newest_year + 1):
                r = client.head(self._url(ctx, year, 1, fips))
                time.sleep(REQUEST_DELAY_S)
                if r.status_code == 200:
                    return year
                if r.status_code != 404:
                    r.raise_for_status()
                log.info("QCEW %s Q1 %s not published; trying next year", fips, year)
        return newest_year

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        counties = sorted(ctx.county_fips)
        if not counties:
            return []
        newest = self._latest_available(ctx, counties[0])
        if ctx.backfill:
            quarters, (y, q) = [], newest
            first_year = self._earliest_year(ctx, counties[0], newest[0])
            while y >= first_year:
                quarters.append((y, q))
                y, q = _prev(y, q)
        else:
            quarters, (y, q) = [], newest
            for _ in range(int(cfg.get("incremental_quarters", 3))):
                quarters.append((y, q))
                y, q = _prev(y, q)
        quarters.sort()
        return [
            FetchSpec(url=self._url(ctx, y, q, fips), key=f"qcew_{fips}_{y}q{q}", suffix=".csv",
                      meta={"year": y, "quarter": q, "fips": fips})
            for (y, q) in quarters for fips in counties
        ]

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        industries: dict[str, str] = ctx.source_config["industries"]
        header: list[str] | None = None
        idx: dict[str, int] = {}
        for offset, line in doc.iter_lines():
            if not line:
                continue
            if header is None:
                header = next(csv.reader([line]))
                idx = {name: i for i, name in enumerate(header)}
                continue
            row = next(csv.reader([line]))
            own, ind_code, agg, size = row[idx["own_code"]], row[idx["industry_code"]], row[idx["agglvl_code"]], row[idx["size_code"]]
            if size != "0" or row[idx["disclosure_code"]].strip() == "N":
                continue
            is_total = (own, ind_code, agg) == ("0", "10", "70")
            is_sector = own == "5" and agg == "74" and ind_code in industries and ind_code != "10"
            if not (is_total or is_sector):
                continue
            geo = ctx.county(row[idx["area_fips"]])
            if geo is None:
                continue
            suffix = industries["10"] if is_total else industries[ind_code]
            year, qtr = int(row[idx["year"]]), int(row[idx["qtr"]])
            first_month = 3 * (qtr - 1) + 1
            end = offset + len(line)

            def mk(indicator: str, period: date, cell: str, _geo=geo, _line=line, _off=offset, _end=end):
                cell = cell.strip()
                if not cell:
                    return None
                return Point(indicator_id=indicator, geo_key=_geo, period=period, value=int(cell) if cell.lstrip("-").isdigit() else float(cell),
                             excerpt=_line, excerpt_start=_off, excerpt_end=_end)

            for m in (1, 2, 3):
                p = mk(f"qcew_employment_{suffix}", date(year, first_month + m - 1, 1), row[idx[f"month{m}_emplvl"]])
                if p is not None:
                    yield p
            if is_total:
                qstart = date(year, first_month, 1)
                for indicator, col in (("qcew_avg_weekly_wage", "avg_wkly_wage"), ("qcew_establishments_total", "qtrly_estabs")):
                    p = mk(indicator, qstart, row[idx[col]])
                    if p is not None:
                        yield p

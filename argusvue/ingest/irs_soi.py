"""IRS SOI county-to-county migration, annual CSVs (`countyinflowYYYY.csv`, `countyoutflowYYYY.csv`).

File layout (identical for every year pair 1112..2223, verified; only zero-padding of FIPS differs):
  inflow : y2_statefips,y2_countyfips,y1_statefips,y1_countyfips,y1_state,y1_countyname,n1,n2,agi
  outflow: y1_statefips,y1_countyfips,y2_statefips,y2_countyfips,y2_state,y2_countyname,n1,n2,agi
The county we report on comes first (destination y2 for inflow, origin y1 for outflow). The row whose *other*
side is 96/000 ("Total Migration-US and Foreign") is the county summary; n1 = returns, n2 = individuals,
agi = adjusted gross income in thousands USD. -1 marks suppressed cells and is skipped.
Pair `1112` means filing years 2011 -> 2012; period = Jan 1 of the y2 year, valid_from = Jan 1 of the y1 year,
valid_to = Dec 31 of the y2 year.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterable
from datetime import date

import httpx

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource
from argusvue.settings import USER_AGENT

log = logging.getLogger(__name__)

_COLS = (("n1", "returns"), ("n2", "individuals"), ("agi", "agi"))
_HEADERS = {
    "inflow": ["y2_statefips", "y2_countyfips", "y1_statefips", "y1_countyfips", "y1_state", "y1_countyname", "n1", "n2", "agi"],
    "outflow": ["y1_statefips", "y1_countyfips", "y2_statefips", "y2_countyfips", "y2_state", "y2_countyname", "n1", "n2", "agi"],
}


def _year(yy: int) -> int:
    """Two-digit year -> full year (pairs span 2011..2099)."""
    return 2000 + yy


def next_pair(pair: str) -> str:
    a, b = int(pair[:2]), int(pair[2:])
    return f"{(a + 1) % 100:02d}{(b + 1) % 100:02d}"


class IrsMigrationSource(StructuredSource):
    slug = "irs_soi"

    # -- discovery --------------------------------------------------------------------------------
    def year_pairs(self, ctx: IngestContext, client: httpx.Client | None = None) -> list[str]:
        """First pair from config, then probe forward with HEAD until the first 404."""
        cfg = ctx.source_config
        base = cfg["base"].rstrip("/")
        pairs = [cfg["first_year_pair"]]
        own = client is None
        client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True)
        try:
            while len(pairs) < 40:
                nxt = next_pair(pairs[-1])
                r = client.head(f"{base}/countyinflow{nxt}.csv")
                if r.status_code == 404:
                    break
                r.raise_for_status()
                pairs.append(nxt)
        finally:
            if own:
                client.close()
        return pairs

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        base = ctx.source_config["base"].rstrip("/")
        pairs = self.year_pairs(ctx)
        if not ctx.backfill:
            pairs = pairs[-2:]
        specs: list[FetchSpec] = []
        for p in pairs:
            for direction in ("inflow", "outflow"):
                specs.append(FetchSpec(url=f"{base}/county{direction}{p}.csv", key=f"{direction}_{p}", suffix=".csv",
                                       meta={"direction": direction, "pair": p}))
        return specs

    # -- parsing ----------------------------------------------------------------------------------
    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        direction = doc.meta.get("direction")
        pair = doc.meta.get("pair")
        if not direction or not pair:
            direction, _, pair = doc.key.partition("_")
        if direction not in _HEADERS:
            raise ValueError(f"unknown IRS direction {direction!r}")
        total = int(ctx.source_config.get("total_row_countyfips", "96000"))
        y1, y2 = _year(int(pair[:2])), _year(int(pair[2:]))
        period, valid_from, valid_to = date(y2, 1, 1), date(y1, 1, 1), date(y2, 12, 31)

        header_seen = False
        for offset, line in doc.iter_lines():
            if not line.strip():
                continue
            row = next(csv.reader([line]))
            if not header_seen:
                if [h.strip() for h in row] != _HEADERS[direction]:
                    raise ValueError(f"unexpected IRS {direction} header for {pair}: {line!r}")
                header_seen = True
                continue
            if len(row) < 9:
                continue
            try:
                own_state, own_county = int(row[0]), int(row[1])
                other = int(row[2]) * 1000 + int(row[3])
            except ValueError:
                continue
            if other != total:
                continue
            geo = ctx.county(f"{own_state:02d}{own_county:03d}")
            if geo is None or not ctx.in_region(geo):
                continue
            end = offset + len(line)
            for col, suffix in zip(row[6:9], ("returns", "individuals", "agi"), strict=True):
                try:
                    value = int(col)
                except ValueError:
                    continue
                if value < 0:
                    continue
                yield Point(indicator_id=f"migration_{direction}_{suffix}", geo_key=geo, period=period, value=value,
                            excerpt=line, excerpt_start=offset, excerpt_end=end,
                            valid_from=valid_from, valid_to=valid_to, attrs={"year_pair": pair})

"""Census Building Permits Survey: monthly place ('we') and county ('co') files, 'c' = with imputation.

Place files (CSV, two header rows + blank line): state FIPS in col 1, FIPS Place Code in col 5 (equals
Census PLACEFP; FIPS place 99990 is the unincorporated remainder and is skipped), place name in col 16, then
12 numeric columns (1-unit, 2-unit, 3-4 unit, 5+ unit x Bldgs/Units/Value). County files: state col 1,
county col 2, name col 5, then 12 estimated columns (followed by 12 'rep' = reported-only columns, ignored).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import date
from urllib.parse import quote

import httpx

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource
from argusvue.settings import USER_AGENT

log = logging.getLogger(__name__)

SKIP_PLACE = "99990"
FORMULA_UNITS_MF = "units_2 + units_3_4 + units_5plus"
FORMULA_BLDGS_MF = "bldgs_2 + bldgs_3_4 + bldgs_5plus"
FORMULA_UNITS_TOTAL = "units_1 + units_2 + units_3_4 + units_5plus"


def _ym(s: str) -> tuple[int, int]:
    y, m = s.split("-")
    return int(y), int(m)


def _months(first: tuple[int, int], last: tuple[int, int]) -> list[tuple[int, int]]:
    out, (y, m) = [], first
    while (y, m) <= last:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _prev(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _int(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


class BpsSource(StructuredSource):
    slug = "census_bps"

    # -- URLs ------------------------------------------------------------------------------------
    def _urls(self, cfg: dict, y: int, m: int) -> tuple[str, str]:
        yymm = f"{y % 100:02d}{m:02d}"
        base = cfg["base"].rstrip("/")
        place_dir = "/".join(quote(p) for p in cfg["place_dir"].split("/"))
        return (f"{base}/{place_dir}/{cfg['place_prefix']}{yymm}c.txt",
                f"{base}/County/{cfg['county_prefix']}{yymm}c.txt")

    def _latest_available(self, ctx: IngestContext, cfg: dict, first: tuple[int, int]) -> tuple[int, int]:
        """Probe backwards from the current month until both the place and county file exist."""
        y, m = ctx.today.year, ctx.today.month
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as client:
            while (y, m) >= first:
                urls = self._urls(cfg, y, m)
                ok = True
                for u in urls:
                    r = client.head(u)
                    time.sleep(1.0)
                    if r.status_code != 200:
                        ok = False
                        break
                if ok:
                    return y, m
                log.info("census_bps: %04d-%02d not published yet", y, m)
                y, m = _prev(y, m)
        raise RuntimeError("census_bps: no monthly BPS file found")

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        first = _ym(cfg["first_period"])
        last = self._latest_available(ctx, cfg, first)
        months = _months(first, last)
        if not ctx.backfill:
            months = months[-int(cfg.get("incremental_months", 4)):]
        specs: list[FetchSpec] = []
        for y, m in months:
            purl, curl = self._urls(cfg, y, m)
            period = date(y, m, 1).isoformat()
            tag = f"{y}{m:02d}"
            specs.append(FetchSpec(url=purl, key=f"place_{tag}", suffix=".txt", title=f"Census BPS place file {tag}",
                                   meta={"period": period, "level": "place"}))
            specs.append(FetchSpec(url=curl, key=f"county_{tag}", suffix=".txt", title=f"Census BPS county file {tag}",
                                   meta={"period": period, "level": "county"}))
        return specs

    # -- parsing ---------------------------------------------------------------------------------
    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        level = doc.meta.get("level") or doc.key.split("_")[0]
        for off, line in doc.iter_lines():
            parts = line.split(",")
            if len(parts) < 3 or not parts[0].strip().isdigit() or len(parts[0].strip()) != 6:
                continue  # header / blank rows
            survey = parts[0].strip()
            period = date(int(survey[:4]), int(survey[4:6]), 1)
            state = parts[1].strip()
            if level == "place":
                if len(parts) < 29:
                    continue
                place = parts[5].strip()
                if not place or place == SKIP_PLACE:
                    continue
                geo = ctx.place(state + place)
                nums = [_int(x) for x in parts[17:29]]
            else:
                if len(parts) < 18:
                    continue
                geo = ctx.county(state + parts[2].strip())
                nums = [_int(x) for x in parts[6:18]]
            if geo is None or any(n is None for n in nums):
                continue
            (b1, u1, v1, b2, u2, _v2, b3, u3, _v3, b5, u5, _v5) = nums
            end = off + len(line)
            vals = [
                ("permits_sf_units", u1, {}),
                ("permits_sf_buildings", b1, {}),
                ("permits_sf_value", v1, {}),
                ("permits_mf_units", u2 + u3 + u5, {"formula": FORMULA_UNITS_MF}),
                ("permits_mf_buildings", b2 + b3 + b5, {"formula": FORMULA_BLDGS_MF}),
                ("permits_total_units", u1 + u2 + u3 + u5, {"formula": FORMULA_UNITS_TOTAL}),
            ]
            for ind, value, attrs in vals:
                yield Point(indicator_id=ind, geo_key=geo, period=period, value=value, excerpt=line,
                            excerpt_start=off, excerpt_end=end, attrs=dict(attrs))

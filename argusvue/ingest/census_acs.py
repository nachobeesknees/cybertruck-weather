"""Census ACS 5-year estimates (population, households, median household income) via the Census API.

Per release year and state: one county request and, for ZCTAs, either a per-state request (years <= 2019,
where ZCTAs nest in states) or one national request per year (years >= 2020, filtered through the gazetteer).
The API returns a JSON array of arrays with one row per line, so each row's raw line is the excerpt.

The Census API now redirects keyless requests to an HTML "Missing Key" page. Set `CENSUS_API_KEY` in the
environment (or name another variable in config `api_key_env`); without it `fetch_specs` fails loudly.
NOTE: the key is part of the request URL and therefore of `documents.url` (see the delivery report).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from datetime import date
from urllib.parse import quote

import httpx

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource
from argusvue.settings import USER_AGENT

log = logging.getLogger(__name__)

REQUEST_DELAY_S = 1.0
PROBE_BACK_YEARS = 3
ZCTA_COL = "zip code tabulation area"
INCREMENTAL_YEARS = 2


class AcsSource(StructuredSource):
    slug = "census_acs"

    # -- request building ---------------------------------------------------------------------
    def _key_param(self, ctx: IngestContext) -> str:
        env = ctx.source_config.get("api_key_env", "CENSUS_API_KEY")
        key = os.environ.get(env)
        return f"&key={key}" if key else ""

    def _url(self, ctx: IngestContext, year: int, get: str, geo: str) -> str:
        return f"{ctx.source_config['base'].rstrip('/')}/{year}/acs/acs5?get={get}&{geo}{self._key_param(ctx)}"

    def _newest_year(self, ctx: IngestContext) -> int:
        state = min(ctx.state_fips)
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as client:
            for year in range(ctx.today.year - 1, ctx.today.year - 1 - PROBE_BACK_YEARS, -1):
                r = client.get(self._url(ctx, year, "NAME", f"for=state:{state}"))
                time.sleep(REQUEST_DELAY_S)
                if r.status_code in (400, 404):
                    continue
                r.raise_for_status()
                if not r.text.lstrip().startswith("["):
                    raise RuntimeError(
                        "Census API did not return JSON (it redirects keyless requests to a 'Missing Key' page): "
                        f"set {ctx.source_config.get('api_key_env', 'CENSUS_API_KEY')} in the environment")
                return year
        raise RuntimeError(f"no ACS 5-year release found for the last {PROBE_BACK_YEARS} years")

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        variables = ",".join(["NAME", *cfg["variables"]])
        newest = self._newest_year(ctx)
        first = int(cfg["first_year"]) if ctx.backfill else newest - INCREMENTAL_YEARS + 1
        specs: list[FetchSpec] = []
        for year in range(max(first, int(cfg["first_year"])), newest + 1):
            for st in sorted(ctx.state_fips):
                counties = sorted(c[2:] for c in ctx.county_fips if c[:2] == st)
                if counties:
                    specs.append(FetchSpec(
                        url=self._url(ctx, year, variables, f"for=county:{','.join(counties)}&in=state:{st}"),
                        key=f"acs_{year}_county_{st}", suffix=".json", meta={"year": year, "level": "county", "state": st}))
                if year <= 2019:
                    specs.append(FetchSpec(
                        url=self._url(ctx, year, variables, f"for={quote(ZCTA_COL)}:*&in=state:{st}"),
                        key=f"acs_{year}_zcta_{st}", suffix=".json", meta={"year": year, "level": "zcta", "state": st}))
            if year >= 2020:
                specs.append(FetchSpec(
                    url=self._url(ctx, year, variables, f"for={quote(ZCTA_COL)}:*"),
                    key=f"acs_{year}_zcta_us", suffix=".json", meta={"year": year, "level": "zcta", "state": None}))
        return specs

    # -- parsing ------------------------------------------------------------------------------
    @staticmethod
    def _row(line: str) -> list | None:
        s = line.strip()
        if s.startswith("[["):
            s = s[1:]
        s = s.rstrip(",")
        if s.endswith("]]"):
            s = s[:-1]
        if not s.startswith("["):
            return None
        return json.loads(s)

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        variables: dict[str, str] = ctx.source_config["variables"]
        year = int(doc.meta["year"])
        period, vfrom, vto = date(year, 1, 1), date(year - 4, 1, 1), date(year, 12, 31)
        idx: dict[str, int] | None = None
        for offset, line in doc.iter_lines():
            row = self._row(line)
            if row is None:
                continue
            if idx is None:
                idx = {name: i for i, name in enumerate(row)}
                missing = [v for v in variables if v not in idx]
                if missing:
                    raise ValueError(f"ACS {year}: variables {missing} missing from header {row}")
                continue
            if ZCTA_COL in idx:
                geo = ctx.zcta(str(row[idx[ZCTA_COL]]))
            elif "county" in idx and "state" in idx:
                geo = ctx.county(str(row[idx["state"]]).zfill(2) + str(row[idx["county"]]).zfill(3))
            else:
                geo = None
            if geo is None:
                continue
            end = offset + len(line)
            for var, indicator in variables.items():
                raw = row[idx[var]]
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if value < 0:           # -666666666 and friends are Census sentinels, not data
                    continue
                yield Point(indicator_id=indicator, geo_key=geo, period=period, value=int(value) if value == int(value) else value,
                            excerpt=line, excerpt_start=offset, excerpt_end=end, valid_from=vfrom, valid_to=vto,
                            attrs={"acs_variable": var, "release_year": year})

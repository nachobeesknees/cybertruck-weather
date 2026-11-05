"""Test helpers shared by ingest parser tests: a DB-free stand-in for IngestContext."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from argusvue.geo.states import FIPS_BY_USPS
from argusvue.ingest.base import FetchedDocument


def _norm(s: str) -> str:
    return " ".join(s.lower().replace(",", " ").split())


class FakeCtx:
    """Duck-types IngestContext for parser unit tests. Give it the gazetteer keys the test needs:
    FakeCtx(zctas=['85085'], counties={'04013': 'Maricopa County'}, places={'0455000': 'Phoenix'},
            cbsa='38060', region_id='phx')."""

    def __init__(self, *, region_id: str = "phx", cbsa: str | None = "38060", zctas=(), counties: dict | None = None,
                 places: dict | None = None, backfill: bool = True, source_config: dict | None = None,
                 containing: dict | None = None, today: date | None = None):
        self.region_id = region_id
        self.backfill = backfill
        self.source_config = source_config or {}
        self.today = today or date.today()
        counties = counties or {}
        places = places or {}
        self.by_level: dict[str, dict[str, str]] = {
            "zcta": {z: f"zcta:{z}" for z in zctas},
            "county": {c: f"county:{c}" for c in counties},
            "place": {p: f"place:{p}" for p in places},
            "cbsa": {cbsa: f"cbsa:{cbsa}"} if cbsa else {},
            "state": {s: f"state:{s}" for s in sorted({c[:2] for c in counties} | {p[:2] for p in places})},
        }
        self.geo_keys = {"country:US"} | {k for lvl in self.by_level.values() for k in lvl.values()}
        self.names = {**{f"county:{c}": n for c, n in counties.items()}, **{f"place:{p}": n for p, n in places.items()}}
        self._county_by_name = {(c[:2], _norm(n)): f"county:{c}" for c, n in counties.items()}
        self._place_by_name = {(p[:2], _norm(n)): f"place:{p}" for p, n in places.items()}
        self._containing = containing or {}
        self._cbsa = cbsa

    # lookups mirroring IngestContext
    def in_region(self, key: str) -> bool:
        return key in self.geo_keys

    def zcta(self, code: str) -> str | None:
        return self.by_level["zcta"].get(code.zfill(5))

    def county(self, fips: str) -> str | None:
        return self.by_level["county"].get(fips.zfill(5))

    def cbsa(self, code: str) -> str | None:
        return self.by_level["cbsa"].get(code)

    def place(self, code7: str) -> str | None:
        return self.by_level["place"].get(code7)

    def place_by_name(self, name: str, state_usps: str) -> str | None:
        fips = FIPS_BY_USPS.get(state_usps.upper())
        return self._place_by_name.get((fips, _norm(name))) if fips else None

    def county_by_name(self, name: str, state_usps: str) -> str | None:
        fips = FIPS_BY_USPS.get(state_usps.upper())
        return self._county_by_name.get((fips, _norm(name))) if fips else None

    def containing(self, lat: float, lon: float, level: str) -> str | None:
        return self._containing.get((round(lat, 4), round(lon, 4), level))

    @property
    def county_fips(self) -> list[str]:
        return list(self.by_level["county"])

    @property
    def state_fips(self) -> list[str]:
        return list(self.by_level["state"])

    @property
    def cbsa_code(self) -> str | None:
        return self._cbsa


def doc_from_file(path: Path, *, key: str = "", url: str = "https://example.test/doc", meta: dict[str, Any] | None = None) -> FetchedDocument:
    """A FetchedDocument wrapping a fixture file on disk (no DB, no blob store)."""
    from datetime import UTC, datetime

    p = Path(path)
    return FetchedDocument(id=0, url=url, key=key, blob_path=p, fetched_at=datetime.now(UTC), content_hash="0" * 64,
                           size=p.stat().st_size, mime=None, meta=meta or {}, is_new=True)

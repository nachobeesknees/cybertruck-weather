from datetime import date
from pathlib import Path

import pytest

from argusvue.ingest.fred import FredSource, seasonal_flag
from tests.helpers import FakeCtx, doc_from_file

FX = Path(__file__).parent / "fixtures" / "fred"
SERIES = [
    {"ids": ["PHOE004URN"], "indicator": "unemployment_rate", "geo": "cbsa:38060"},
    {"ids": ["WYTETO9URN"], "indicator": "unemployment_rate", "geo": "county:56039"},
    {"ids": ["PHOE004LEIHN", "PHOE004LEIH"], "indicator": "nonfarm_jobs_leisure_hospitality", "geo": "cbsa:38060"},
]
CFG = {"base": "https://fred.test/csv?id=", "series": SERIES}


def _parse(name, ind, geo, region_counties=None):
    ctx = FakeCtx(counties=region_counties or {"04013": "Maricopa"}, source_config=CFG)
    doc = doc_from_file(FX / f"{name}.csv", meta={"series_id": name, "indicator": ind, "geo": geo})
    pts = list(FredSource().parse(doc, ctx))
    for p in pts:
        assert doc.text()[p.excerpt_start:p.excerpt_end] == p.excerpt
    return pts


def test_unemployment_skips_missing_dot():
    pts = _parse("PHOE004URN", "unemployment_rate", "cbsa:38060")
    assert [(p.period, p.value) for p in pts] == [(date(1990, 1, 1), 4.6), (date(1990, 2, 1), 4.3), (date(2026, 8, 1), 4.9), (date(2026, 9, 1), 4.2)]
    assert pts[0].attrs == {"series_id": "PHOE004URN", "seasonal": "NSA"} and pts[0].excerpt == "1990-01-01,4.6"


def test_sa_fallback_series_flagged():
    pts = _parse("PHOE004LEIH", "nonfarm_jobs_leisure_hospitality", "cbsa:38060")
    assert pts[0].attrs["seasonal"] == "SA" and pts[0].value == 103.0453239595


def test_out_of_region_geo_yields_nothing():
    assert _parse("PHOE004URN", "unemployment_rate", "county:56039") == []


def test_seasonal_flag():
    assert [seasonal_flag(s) for s in ("PHOE004NAN", "SMU04380602000000001", "PHOE004NA", "AZMARI3URN")] == ["NSA", "NSA", "SA", "NSA"]


def test_header_mismatch_fails(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("observation_date,OTHER\n1990-01-01,1\n")
    with pytest.raises(ValueError, match="header"):
        list(FredSource().parse(doc_from_file(f, meta={"series_id": "X", "indicator": "i", "geo": "country:US"}), FakeCtx(source_config=CFG)))


class _Stream:
    def __init__(self, code): self.status_code = code
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): assert self.status_code < 400


class _Client:
    def __init__(self, missing): self.missing, self.urls = missing, []
    def stream(self, method, url):
        self.urls.append(url)
        return _Stream(404 if url.rsplit("=", 1)[1] in self.missing else 200)
    def close(self): pass


def test_fetch_specs_falls_back_and_filters_region():
    ctx = FakeCtx(counties={"04013": "Maricopa"}, source_config=CFG)       # cbsa:38060 in, county:56039 out
    client = _Client({"PHOE004LEIHN"})
    specs = FredSource().fetch_specs(ctx, client=client, delay_s=0)
    assert [s.meta["series_id"] for s in specs] == ["PHOE004URN", "PHOE004LEIH"]
    assert specs[1].key == "nonfarm_jobs_leisure_hospitality|cbsa:38060" and specs[1].suffix == ".csv"
    assert specs[1].meta["candidates"] == ["PHOE004LEIHN", "PHOE004LEIH"]
    assert "https://fred.test/csv?id=WYTETO9URN" not in client.urls


def test_fetch_specs_all_missing_raises():
    ctx = FakeCtx(source_config=CFG)
    with pytest.raises(RuntimeError, match="none of the FRED candidates"):
        FredSource().fetch_specs(ctx, client=_Client({"PHOE004URN"}), delay_s=0)

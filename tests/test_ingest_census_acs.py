"""ACS parser + spec generation.

The Census API requires a key (keyless requests redirect to an HTML page), so none was available when
these were written: the fixtures are small hand-built responses in the exact API wire format (JSON array
of arrays, one row per line; Maricopa figures from the task brief, others illustrative). Re-capture real
responses once CENSUS_API_KEY is set if tighter fidelity is wanted.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from argusvue.ingest.census_acs import AcsSource
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "census_acs"
CFG = {"base": "https://api.census.gov/data", "first_year": 2011,
       "variables": {"B01003_001E": "population", "B11001_001E": "households", "B19013_001E": "median_hh_income"}}


def _ctx(**kw):
    return FakeCtx(zctas=["85085", "83001", "85001"], counties={"04013": "Maricopa County", "04021": "Pinal County", "56039": "Teton County"},
                   source_config=CFG, **kw)


def _parse(fname, year, **kw):
    doc = doc_from_file(FIX / fname, meta={"year": year, **kw})
    pts = list(AcsSource().parse(doc, _ctx()))
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
    return pts, {(p.indicator_id, p.geo_key): p for p in pts}


def test_county_rows():
    pts, d = _parse("county_04_2022.json", 2022)
    assert {p.geo_key for p in pts} == {"county:04013", "county:04021"}      # Pima is out of region
    p = d[("population", "county:04013")]
    assert p.value == 4420568 and p.period == date(2022, 1, 1)
    assert p.valid_from == date(2018, 1, 1) and p.valid_to == date(2022, 12, 31)
    assert d[("households", "county:04013")].value == 1600000
    assert d[("median_hh_income", "county:04013")].value == 72944
    assert p.excerpt == '["Maricopa County, Arizona","4420568","1600000","72944","04","013"],'
    assert len(pts) == 6


def test_sentinel_and_null_values_skipped():
    pts, d = _parse("county_56_2022.json", 2022)
    assert {p.geo_key for p in pts} == {"county:56039"}
    assert ("median_hh_income", "county:56039") not in d
    assert d[("population", "county:56039")].value == 23331 and len(pts) == 2


def test_national_zcta_filtered_through_gazetteer():
    pts, d = _parse("zcta_us_2022.json", 2022)
    assert {p.geo_key for p in pts} == {"zcta:85085", "zcta:83001", "zcta:85001"}
    assert d[("median_hh_income", "zcta:85085")].value == 135000
    assert d[("population", "zcta:83001")].value == 11800
    assert ("median_hh_income", "zcta:85001") not in d       # sentinel
    # the last row of a response ends with "]]"; the raw line is still the excerpt and still parses
    assert d[("population", "zcta:85001")].excerpt == '["ZCTA5 85001","1200","600","-666666666","85001"]]'


def test_per_state_zcta_2019():
    _pts, d = _parse("zcta_04_2019.json", 2019)
    assert d[("population", "zcta:85085")].value == 24500
    assert d[("population", "zcta:85085")].valid_from == date(2015, 1, 1)
    assert d[("median_hh_income", "zcta:85001")].period == date(2019, 1, 1)


def test_missing_variable_header_fails(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('[["NAME","B01003_001E","state","county"],\n["A","1","04","013"]]')
    with pytest.raises(ValueError, match="missing"):
        list(AcsSource().parse(doc_from_file(p, meta={"year": 2022}), _ctx()))


def test_fetch_specs(monkeypatch):
    monkeypatch.setattr(AcsSource, "_newest_year", lambda self, ctx: 2021)
    monkeypatch.setenv("CENSUS_API_KEY", "K")
    ctx = FakeCtx(region_id="jh", counties={"56039": "Teton", "16081": "Teton ID", "56023": "Lincoln"}, source_config=CFG)
    specs = {s.key: s for s in AcsSource().fetch_specs(ctx)}
    # 2011..2019: per state county + zcta; 2020..2021: per state county + one national zcta
    assert len(specs) == 9 * 2 * 2 + 2 * (2 + 1)
    s = specs["acs_2015_county_56"]
    assert s.url == ("https://api.census.gov/data/2015/acs/acs5?get=NAME,B01003_001E,B11001_001E,B19013_001E"
                     "&for=county:023,039&in=state:56&key=K")
    assert specs["acs_2015_zcta_16"].url.endswith("&for=zip%20code%20tabulation%20area:*&in=state:16&key=K")
    assert specs["acs_2021_zcta_us"].url.endswith("&for=zip%20code%20tabulation%20area:*&key=K")
    assert "acs_2021_zcta_56" not in specs
    inc = AcsSource().fetch_specs(FakeCtx(counties={"04013": "M"}, source_config=CFG, backfill=False))
    assert sorted({s.meta["year"] for s in inc}) == [2020, 2021]

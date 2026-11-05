from __future__ import annotations

from datetime import date
from pathlib import Path

from argusvue.ingest.census_bps import BpsSource
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "census_bps"
PLACES = {"0455000": "Phoenix", "0446000": "Mesa", "0427400": "Gilbert", "5640120": "Jackson"}
COUNTIES = {"04013": "Maricopa County", "04021": "Pinal County", "56039": "Teton County", "56023": "Lincoln County",
            "16081": "Teton County"}


def _ctx(**kw):
    return FakeCtx(places=PLACES, counties=COUNTIES, **kw)


def _points(name: str, key: str, level: str):
    doc = doc_from_file(FIX / name, key=key, meta={"period": "2026-09-01", "level": level})
    return doc, list(BpsSource().parse(doc, _ctx()))


def _by(points):
    return {(p.indicator_id, p.geo_key): p for p in points}


def test_place_values_and_summed_multifamily():
    _doc, pts = _points("we2609c_sample.txt", "place_202609", "place")
    by = _by(pts)
    ph = "place:0455000"
    assert all(p.period == date(2026, 9, 1) for p in pts)
    assert by[("permits_sf_units", ph)].value == 106
    assert by[("permits_sf_buildings", ph)].value == 106
    assert by[("permits_sf_value", ph)].value == 63195582
    mf = by[("permits_mf_units", ph)]
    assert mf.value == 18 + 3 + 0
    assert mf.attrs == {"formula": "units_2 + units_3_4 + units_5plus"}
    assert by[("permits_mf_buildings", ph)].value == 9 + 1 + 0
    tot = by[("permits_total_units", ph)]
    assert tot.value == 106 + 18 + 3 + 0
    assert "formula" in tot.attrs
    assert by[("permits_sf_units", "place:0446000")].value == 21
    assert by[("permits_sf_units", "place:0427400")].value == 30
    assert by[("permits_sf_value", "place:5640120")].value == 2250000
    assert by[("permits_mf_units", "place:5640120")].value == 0


def test_place_skips_and_excerpts():
    doc, pts = _points("we2609c_sample.txt", "place_202609", "place")
    keys = {p.geo_key for p in pts}
    # Oregon Phoenix (41/57500) and California Jackson (06/36980) are not in the gazetteer; 99990 skipped
    assert keys == {"place:0455000", "place:0446000", "place:0427400", "place:5640120"}
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
        assert "," in p.excerpt and p.excerpt.startswith("202609,")


def test_county_values():
    doc, pts = _points("co2609c_sample.txt", "county_202609", "county")
    by = _by(pts)
    m = "county:04013"
    assert by[("permits_sf_units", m)].value == 904
    assert by[("permits_sf_value", m)].value == 324932957
    assert by[("permits_mf_units", m)].value == 20 + 15 + 106
    assert by[("permits_mf_buildings", m)].value == 10 + 4 + 9
    assert by[("permits_total_units", m)].value == 904 + 20 + 15 + 106
    assert by[("permits_sf_units", "county:04021")].value == 398
    assert by[("permits_mf_units", "county:04021")].value == 14 + 0 + 19
    assert by[("permits_sf_value", "county:56039")].value == 16069862
    assert by[("permits_sf_units", "county:16081")].value == 18
    assert by[("permits_mf_units", "county:16081")].value == 10
    # Lincoln WY present; Colorado Lincoln (08073) absent from gazetteer -> skipped
    assert by[("permits_sf_units", "county:56023")].value == 19
    assert {p.geo_key for p in pts} == {f"county:{c}" for c in COUNTIES}
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt


def test_fetch_specs_enumeration(monkeypatch):
    src = BpsSource()
    monkeypatch.setattr(src, "_latest_available", lambda ctx, cfg, first: (2026, 9))
    cfg = {"base": "https://www2.census.gov/econ/bps", "place_dir": "Place/West Region", "place_prefix": "we",
           "county_prefix": "co", "first_period": "2010-01", "incremental_months": 4}
    full = src.fetch_specs(FakeCtx(source_config=cfg, backfill=True))
    assert len(full) == 2 * ((2026 - 2010) * 12 + 9)
    inc = src.fetch_specs(FakeCtx(source_config=cfg, backfill=False))
    assert [s.key for s in inc] == ["place_202606", "county_202606", "place_202607", "county_202607",
                                    "place_202608", "county_202608", "place_202609", "county_202609"]
    assert inc[-2].url == "https://www2.census.gov/econ/bps/Place/West%20Region/we2609c.txt"
    assert inc[-1].url == "https://www2.census.gov/econ/bps/County/co2609c.txt"
    assert inc[-1].meta == {"period": "2026-09-01", "level": "county"} and inc[-1].suffix == ".txt"

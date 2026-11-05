"""PEP wide-CSV parser on trimmed real vintage files (county rows for Maricopa, Pinal, Teton, plus out-of-region)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from argusvue.ingest.census_pep import PepSource
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "census_pep"
BASE = "https://www2.census.gov/programs-surveys/popest/datasets"
CFG = {
    "files": [
        {"vintage": 2025, "url": f"{BASE}/2020-2025/counties/totals/co-est2025-alldata.csv", "years": [2020, 2025]},
        {"vintage": 2019, "url": f"{BASE}/2010-2019/counties/totals/co-est2019-alldata.csv", "years": [2010, 2019]},
        {"vintage": 2024, "url": f"{BASE}/2020-2024/counties/totals/co-est2024-alldata.csv", "years": [2020, 2024]},
    ],
    "encoding": "latin-1",
    "columns": {"POPESTIMATE": "pop_estimate", "NETMIG": "net_migration", "DOMESTICMIG": "domestic_migration",
                "INTERNATIONALMIG": "international_migration"},
}


def _ctx():
    return FakeCtx(counties={"04013": "Maricopa County", "04021": "Pinal County", "56039": "Teton County"}, source_config=CFG)


def _parse(fname, vintage, years):
    doc = doc_from_file(FIX / fname, key=f"pep_vintage{vintage}", meta={"vintage": vintage, "years": years})
    pts = list(PepSource().parse(doc, _ctx()))
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
    return pts, {(p.indicator_id, p.geo_key, p.period): p for p in pts}


def test_fetch_specs_oldest_vintage_first():
    specs = PepSource().fetch_specs(_ctx())
    assert [s.meta["vintage"] for s in specs] == [2019, 2024, 2025]
    assert specs[-1].url.endswith("co-est2025-alldata.csv") and specs[0].meta["years"] == [2010, 2019]


def test_vintage_2019_values_and_attrs():
    pts, d = _parse("co-est2019.csv", 2019, [2010, 2019])
    assert {p.geo_key for p in pts} == {"county:04013", "county:04021", "county:56039"}
    assert len(pts) == 3 * 4 * 10
    assert d[("pop_estimate", "county:04013", date(2010, 1, 1))].value == 3825110
    assert d[("pop_estimate", "county:04013", date(2019, 1, 1))].value == 4485414
    assert d[("net_migration", "county:56039", date(2010, 1, 1))].value == -71
    assert d[("domestic_migration", "county:04013", date(2019, 1, 1))].value == 57477
    assert d[("international_migration", "county:04013", date(2010, 1, 1))].value == 1788
    assert pts[0].attrs == {"reference": "July 1", "vintage": 2019}


def test_newer_vintages_revise_overlapping_year():
    _, d24 = _parse("co-est2024.csv", 2024, [2020, 2024])
    _, d25 = _parse("co-est2025.csv", 2025, [2020, 2025])
    k = ("pop_estimate", "county:04013", date(2020, 1, 1))
    assert d24[k].value == 4445415 and d25[k].value == 4445025
    assert d24[k].attrs["vintage"] == 2024 and d25[k].attrs["vintage"] == 2025
    assert ("pop_estimate", "county:04013", date(2025, 1, 1)) not in d24
    assert d25[("pop_estimate", "county:56039", date(2025, 1, 1))].value == 23333
    assert d25[("net_migration", "county:56039", date(2025, 1, 1))].value == -135


def test_state_rows_and_out_of_region_counties_are_skipped():
    pts, _ = _parse("co-est2025.csv", 2025, [2020, 2025])
    assert all(p.geo_key.startswith("county:") for p in pts)
    assert {p.geo_key for p in pts} == {"county:04013", "county:04021", "county:56039"}   # no 040 Arizona row, no Autauga/Pima


def test_one_raw_line_serves_all_points_of_a_county():
    pts, _ = _parse("co-est2025.csv", 2025, [2020, 2025])
    lines = {p.excerpt for p in pts if p.geo_key == "county:04013"}
    assert len(lines) == 1 and next(iter(lines)).startswith("050,4,8,04,013,Arizona,Maricopa County")

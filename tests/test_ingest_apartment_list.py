from datetime import date
from pathlib import Path

import pytest

from argusvue.ingest.apartment_list import ApartmentListSource, find_csv_url
from tests.helpers import FakeCtx, doc_from_file

FX = Path(__file__).parent / "fixtures" / "apartment_list"
PATTERN = r"Apartment_List_Rent_Estimates_\d{4}_\d{2}\.csv"


def test_extract_newest_csv_url():
    html = (FX / "research_page.html").read_text()
    url, ym = find_csv_url(html, "assets.ctfassets.net", PATTERN)
    assert ym == "2026_10"
    assert url == ("https://assets.ctfassets.net/jeox55pd4d8n/22URKuUw4Fjzg4cODP5YBf/"
                   "e2ca9fa8c46588a7ee139301984b108a/Apartment_List_Rent_Estimates_2026_10.csv")


def test_extract_fails_when_absent():
    with pytest.raises(RuntimeError):
        find_csv_url("<html></html>", "assets.ctfassets.net", PATTERN)


def _points():
    ctx = FakeCtx(counties={"04013": "Maricopa County", "04021": "Pinal County"}, places={"0455000": "Phoenix"})
    doc = doc_from_file(FX / "rent_estimates.csv", key="rent_estimates")
    pts = list(ApartmentListSource().parse(doc, ctx))
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
    return pts, {(p.indicator_id, p.geo_key, p.period): p for p in pts}


def test_geo_levels_and_values():
    pts, by = _points()
    assert {p.geo_key for p in pts} == {"cbsa:38060", "county:04013", "county:04021", "place:0455000"}
    assert {p.indicator_id for p in pts} == {"aptlist_rent_overall", "aptlist_rent_1br", "aptlist_rent_2br"}
    assert by[("aptlist_rent_overall", "cbsa:38060", date(2017, 1, 1))].value == 941
    assert by[("aptlist_rent_1br", "county:04013", date(2017, 1, 1))].value == 798
    assert by[("aptlist_rent_2br", "place:0455000", date(2017, 2, 1))].value == 897
    assert by[("aptlist_rent_overall", "cbsa:38060", date(2017, 1, 1))].excerpt.startswith('"Phoenix-Mesa-Chandler, AZ","Metro",38060')


def test_blank_cells_skipped(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text('"location_name","location_type","location_fips_code","population","state","county","metro","bed_size","2017_01","2017_02","2017_03"\n'
                 '"Pinal County, AZ","County",4021,433338,"Arizona","Pinal County","x","1br",432,,437\n'
                 '"Pinal County, AZ","County",4021,433338,"Arizona","Pinal County","x","studio",1,2,3\n')
    ctx = FakeCtx(counties={"04021": "Pinal County"})
    pts = list(ApartmentListSource().parse(doc_from_file(f), ctx))
    assert [(p.period, p.value) for p in pts] == [(date(2017, 1, 1), 432), (date(2017, 3, 1), 437)]   # blank and studio skipped


def test_latest_period():
    pts, _ = _points()
    assert max(p.period for p in pts) == date(2026, 10, 1) and min(p.period for p in pts) == date(2017, 1, 1)


def test_region_filter():
    ctx = FakeCtx(counties={"04013": "Maricopa County"})          # no Pinal, no Phoenix place
    pts = list(ApartmentListSource().parse(doc_from_file(FX / "rent_estimates.csv"), ctx))
    assert {p.geo_key for p in pts} == {"cbsa:38060", "county:04013"}

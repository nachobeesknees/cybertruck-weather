"""Redfin parser on trimmed real CSVs (real header + a few rows for our geographies)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from argusvue.ingest.redfin import RedfinSource
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "redfin"
CFG = {
    "base": "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_data_center",
    "files": {"metros": "housing_market/monthly/all_metros.csv", "zips": "housing_market/monthly/all_zips.csv"},
    "frequency_filter": "Monthly",
    "columns": {
        "median_sale_price": "MEDIAN SALE PRICE NSA ($)", "median_dom": "MEDIAN DAYS ON MARKET (DAYS)",
        "active_inventory": "INVENTORY", "months_of_supply": "MONTHS OF SUPPLY", "new_listings": "NEW LISTINGS",
        "homes_sold": "HOMES SOLD", "sale_to_list": "AVERAGE SALE TO LIST RATIO (%)",
        "share_sold_above_list": "SHARE SOLD ABOVE ORIGINAL LIST (%)",
        "pct_off_market_2w": "PERCENT OFF MARKET IN TWO WEEKS (%)",
    },
}


def _ctx(**kw):
    kw.setdefault("source_config", CFG)
    kw.setdefault("zctas", ["85085", "83001"])
    kw.setdefault("counties", {"04013": "Maricopa County", "56039": "Teton County"})
    kw.setdefault("places", {"0455000": "Phoenix"})
    return FakeCtx(**kw)


def _parse(name, level, ctx=None):
    doc = doc_from_file(FIX / name, key=level, meta={"level": level})
    pts = list(RedfinSource().parse(doc, ctx or _ctx()))
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
    return pts


def _by(pts, ind, geo):
    return {p.period: p.value for p in pts if p.indicator_id == ind and p.geo_key == geo}


def test_fetch_specs_one_per_file():
    specs = RedfinSource().fetch_specs(_ctx())
    assert [s.key for s in specs] == ["metros", "zips"]
    assert specs[0].url == "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_data_center/housing_market/monthly/all_metros.csv"
    assert specs[0].meta == {"level": "metros"} and specs[0].suffix == ".csv"


def test_metros_by_region_id_phoenix():
    pts = _parse("metros.csv", "metros")
    assert {p.geo_key for p in pts} == {"cbsa:38060"}  # Jackson (27220) and Aberdeen are not this ctx's CBSA
    sep = date(2026, 9, 1)
    assert _by(pts, "median_sale_price", "cbsa:38060")[sep] == 458944
    assert _by(pts, "homes_sold", "cbsa:38060")[sep] == 5601
    assert _by(pts, "median_dom", "cbsa:38060")[sep] == 65
    assert _by(pts, "sale_to_list", "cbsa:38060")[sep] == 97.9
    assert _by(pts, "active_inventory", "cbsa:38060")[sep] == 26399
    assert _by(pts, "months_of_supply", "cbsa:38060")[sep] == 4.6
    assert sorted(_by(pts, "median_sale_price", "cbsa:38060")) == [date(2026, m, 1) for m in (7, 8, 9)]
    assert len(pts) == 3 * 9


def test_metros_jackson_skips_na_cells():
    pts = _parse("metros.csv", "metros", _ctx(region_id="jh", cbsa="27220", zctas=[], counties={}, places={}))
    assert {p.geo_key for p in pts} == {"cbsa:27220"}
    assert _by(pts, "median_sale_price", "cbsa:27220")[date(2026, 9, 1)] == 1346408
    assert _by(pts, "share_sold_above_list", "cbsa:27220")[date(2026, 9, 1)] == 0  # real zero is kept
    assert "sale_to_list" not in {p.indicator_id for p in pts}  # NA in every Jackson row
    assert all(p.value is not None for p in pts)


def test_counties_by_name_and_skip_others():
    pts = _parse("counties.csv", "counties")
    assert {p.geo_key for p in pts} == {"county:04013", "county:56039"}  # Bergen/Bronx skipped
    assert _by(pts, "homes_sold", "county:56039")[date(2026, 9, 1)] == 25
    assert sorted(_by(pts, "homes_sold", "county:04013")) == [date(2026, m, 1) for m in (7, 8, 9)]
    only_az = _parse("counties.csv", "counties", _ctx(counties={"04013": "Maricopa County"}))
    assert {p.geo_key for p in only_az} == {"county:04013"}


def test_rolling_three_month_rows_are_skipped_by_default():
    # the real zip and city "monthly" files contain only `Rolling 3 Months` rows
    assert _parse("zips.csv", "zips") == []
    assert _parse("cities.csv", "cities") == []


def test_zip_and_city_matching_when_frequency_overridden():
    cfg = {**CFG, "frequency_filter": "Rolling 3 Months"}
    zips = _parse("zips.csv", "zips", _ctx(source_config=cfg))
    assert {p.geo_key for p in zips} == {"zcta:85085", "zcta:83001"}  # 10001 skipped
    assert all(p.period == date(2026, 7, 1) for p in zips)
    cities = _parse("cities.csv", "cities", _ctx(source_config=cfg))
    assert {p.geo_key for p in cities} == {"place:0455000"}  # Denver and Jackson WY not in ctx
    assert _by(cities, "homes_sold", "place:0455000")[date(2026, 7, 1)] == 3810


def test_zip_code_prefix_form(tmp_path):
    src = (FIX / "zips.csv").read_text().replace('"Zip","85085"', '"Zip","Zip Code: 85085"')
    p = tmp_path / "z.csv"
    p.write_text(src)
    cfg = {**CFG, "frequency_filter": "Rolling 3 Months"}
    pts = list(RedfinSource().parse(doc_from_file(p, key="zips", meta={"level": "zips"}), _ctx(source_config=cfg)))
    assert "zcta:85085" in {x.geo_key for x in pts}

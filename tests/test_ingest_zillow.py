"""Zillow parser on trimmed real CSVs (header + a few region rows, last six month columns)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from argusvue.ingest.zillow import ZillowSource
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "zillow"
CFG = {
    "base": "https://files.zillowstatic.com/research/public_csvs",
    "metro_to_cbsa": {"Phoenix, AZ": "38060", "Jackson, WY": "27220"},
    "files": {"zhvi": {"metro": "zhvi/m.csv", "zip": "zhvi/z.csv"}, "zillow_inventory": {"metro": "invt/m.csv"}},
}
PERIODS = [date(2026, m, 1) for m in range(4, 10)]


def _check_excerpts(doc, pts):
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt


def test_fetch_specs_one_per_file():
    ctx = FakeCtx(source_config=CFG)
    specs = ZillowSource().fetch_specs(ctx)
    assert [s.key for s in specs] == ["zhvi_metro", "zhvi_zip", "zillow_inventory_metro"]
    assert specs[0].url == "https://files.zillowstatic.com/research/public_csvs/zhvi/m.csv"
    assert all(s.suffix == ".csv" for s in specs)


def test_metro_rows_map_to_cbsa_and_skip_others():
    ctx = FakeCtx(cbsa="38060", source_config=CFG)
    doc = doc_from_file(FIX / "zhvi_metro.csv", key="zhvi_metro", meta={"indicator": "zhvi", "level": "metro"})
    pts = list(ZillowSource().parse(doc, ctx))
    assert {p.geo_key for p in pts} == {"cbsa:38060"}  # Jackson not in this ctx's cbsa map; NY/Denver unmapped
    assert [p.period for p in pts] == PERIODS
    assert all(p.indicator_id == "zhvi" for p in pts)
    assert pts[0].value == 446167.3695088886 and pts[-1].value == 441097.1462944464
    _check_excerpts(doc, pts)
    assert pts[0].excerpt.startswith('394976,11,"Phoenix, AZ",msa,AZ,')


def test_jackson_metro():
    ctx = FakeCtx(region_id="jh", cbsa="27220", source_config=CFG)
    doc = doc_from_file(FIX / "zhvi_metro.csv", key="zhvi_metro", meta={"indicator": "zhvi", "level": "metro"})
    pts = list(ZillowSource().parse(doc, ctx))
    assert {p.geo_key for p in pts} == {"cbsa:27220"} and len(pts) == 6
    assert pts[-1].value == 1437098.182059329
    _check_excerpts(doc, pts)


def test_zip_rows_use_zcta_and_skip_out_of_region():
    ctx = FakeCtx(zctas=["85085"], source_config=CFG)
    doc = doc_from_file(FIX / "zhvi_zip.csv", key="zhvi_zip", meta={"indicator": "zhvi", "level": "zip"})
    pts = list(ZillowSource().parse(doc, ctx))
    assert {p.geo_key for p in pts} == {"zcta:85085"} and [p.period for p in pts] == PERIODS
    assert pts[0].value == 640277.2867923424
    _check_excerpts(doc, pts)
    ctx2 = FakeCtx(region_id="jh", cbsa="27220", zctas=["83001"], source_config=CFG)
    pts2 = list(ZillowSource().parse(doc, ctx2))
    assert {p.geo_key for p in pts2} == {"zcta:83001"} and pts2[-1].value == 2012592.9060742937


def test_blank_cells_skipped_and_key_fallback(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text('RegionID,SizeRank,RegionName,RegionType,StateName,2026-01-31,2026-02-28,2026-03-31\n'
                 '1,1,"Phoenix, AZ",msa,AZ,,12.5,\n')
    doc = doc_from_file(p, key="zillow_inventory_metro")  # no meta: indicator/level derived from key
    pts = list(ZillowSource().parse(doc, FakeCtx(source_config=CFG)))
    assert [(x.indicator_id, x.period, x.value) for x in pts] == [("zillow_inventory", date(2026, 2, 1), 12.5)]
    _check_excerpts(doc, pts)

"""QCEW parser + spec generation on trimmed real county-quarter files (2024 Q1)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from argusvue.ingest.bls_qcew import QcewSource
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "bls_qcew"
CFG = {"base": "https://data.bls.gov/cew/data/api", "first_year": 2010, "incremental_quarters": 3,
       "industries": {"10": "total", "23": "construction", "31-33": "manufacturing", "53": "real_estate", "72": "accommodation_food"}}


def _ctx(**kw):
    return FakeCtx(counties={"04013": "Maricopa County", "04021": "Pinal County", "56039": "Teton County"},
                   source_config=CFG, **kw)


def _parse(fips, ctx=None, path=None):
    ctx = ctx or _ctx()
    doc = doc_from_file(path or FIX / f"{fips}_2024q1.csv", key=f"qcew_{fips}_2024q1")
    pts = list(QcewSource().parse(doc, ctx))
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
    return pts


def _by(pts):
    return {(p.indicator_id, p.period): p for p in pts}


def test_maricopa_total_and_sectors():
    pts = _parse("04013")
    d = _by(pts)
    assert {p.geo_key for p in pts} == {"county:04013"}
    assert d[("qcew_employment_total", date(2024, 1, 1))].value == 2278220
    assert d[("qcew_employment_total", date(2024, 2, 1))].value == 2294557
    assert d[("qcew_employment_total", date(2024, 3, 1))].value == 2298065
    assert d[("qcew_avg_weekly_wage", date(2024, 1, 1))].value == 1457
    assert d[("qcew_establishments_total", date(2024, 1, 1))].value == 145661
    assert d[("qcew_employment_construction", date(2024, 3, 1))].value == 175973
    assert d[("qcew_employment_manufacturing", date(2024, 1, 1))].value == 140882
    assert d[("qcew_employment_real_estate", date(2024, 2, 1))].value == 47181
    assert d[("qcew_employment_accommodation_food", date(2024, 3, 1))].value == 216161
    # 5 employment series x 3 months + wage + establishments; no other-sector or ownership rows
    assert len(pts) == 17
    assert len({(p.indicator_id, p.geo_key, p.period) for p in pts}) == len(pts)


def test_one_raw_line_serves_all_points_of_a_row():
    pts = [p for p in _parse("04013") if p.indicator_id.startswith("qcew_") and p.period.month in (1, 2, 3)]
    total_lines = {p.excerpt for p in pts if p.indicator_id in ("qcew_employment_total", "qcew_avg_weekly_wage", "qcew_establishments_total")}
    assert len(total_lines) == 1 and next(iter(total_lines)).startswith('"04013","0","10","70"')


def test_teton_and_pinal_geo_keys():
    assert {p.geo_key for p in _parse("56039")} == {"county:56039"}
    assert _by(_parse("04021"))[("qcew_employment_total", date(2024, 1, 1))].value == 76565


def test_out_of_region_county_is_skipped():
    assert _parse("04019") == []


def test_suppressed_rows_are_skipped(tmp_path):
    src = (FIX / "56039_2024q1.csv").read_text()
    # flag the construction row as suppressed
    mutated = src.replace('"56039","5","23","74","0","2024","1",""', '"56039","5","23","74","0","2024","1","N"')
    assert mutated != src
    p = tmp_path / "t.csv"
    p.write_text(mutated)
    pts = _parse("56039", path=p)
    assert not [x for x in pts if x.indicator_id == "qcew_employment_construction"]
    assert [x for x in pts if x.indicator_id == "qcew_employment_manufacturing"]


def test_fetch_specs_backfill_and_incremental(monkeypatch):
    monkeypatch.setattr(QcewSource, "_latest_available", lambda self, ctx, fips: (2025, 3))
    monkeypatch.setattr(QcewSource, "_earliest_year", lambda self, ctx, fips, newest: 2014)
    ctx = _ctx(backfill=True)
    specs = QcewSource().fetch_specs(ctx)
    keys = [s.key for s in specs]
    assert len(specs) == 3 * ((2025 - 2014) * 4 + 3)
    assert keys[0] == "qcew_04013_2014q1" and keys[-1] == "qcew_56039_2025q3"
    assert specs[0].url == "https://data.bls.gov/cew/data/api/2014/1/area/04013.csv"
    assert specs[0].meta == {"year": 2014, "quarter": 1, "fips": "04013"}
    inc = QcewSource().fetch_specs(_ctx(backfill=False))
    assert sorted({(s.meta["year"], s.meta["quarter"]) for s in inc}) == [(2025, 1), (2025, 2), (2025, 3)]
    assert len(inc) == 9

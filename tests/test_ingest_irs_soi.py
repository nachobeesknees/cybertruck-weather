from datetime import date
from pathlib import Path

import pytest

from argusvue.ingest.irs_soi import IrsMigrationSource, next_pair
from tests.helpers import FakeCtx, doc_from_file

FX = Path(__file__).parent / "fixtures" / "irs_soi"
CFG = {"base": "https://irs.test", "first_year_pair": "1112", "total_row_countyfips": "96000"}
COUNTIES = {"04013": "Maricopa County", "04021": "Pinal County"}


def _parse(direction, pair, counties=COUNTIES):
    ctx = FakeCtx(counties=counties, source_config=CFG)
    doc = doc_from_file(FX / f"county{direction}{pair}.csv", key=f"{direction}_{pair}",
                        meta={"direction": direction, "pair": pair})
    pts = list(IrsMigrationSource().parse(doc, ctx))
    for p in pts:
        assert doc.text()[p.excerpt_start:p.excerpt_end] == p.excerpt
    return pts, {(p.indicator_id, p.geo_key): p for p in pts}


def test_inflow_padded_fips_and_dates():
    pts, by = _parse("inflow", "1112")
    assert len(pts) == 6                                   # 2 counties x 3 measures; Teton out of region, -1 row skipped
    p = by[("migration_inflow_returns", "county:04013")]
    assert p.value == 76066 and p.period == date(2012, 1, 1)
    assert (p.valid_from, p.valid_to) == (date(2011, 1, 1), date(2012, 12, 31))
    assert by[("migration_inflow_individuals", "county:04013")].value == 148511
    assert by[("migration_inflow_agi", "county:04021")].value == 588000
    assert p.excerpt == "04,013,96,000,AZ,Maricopa County Total Migration-US and Foreign,76066,148511,4062633"


def test_unpadded_fips_outflow_2122():
    pts, by = _parse("outflow", "2122")
    p = by[("migration_outflow_individuals", "county:04013")]
    assert p.value == 155671 and p.period == date(2022, 1, 1) and p.valid_from == date(2021, 1, 1)
    assert by[("migration_outflow_agi", "county:04021")].value == 904142
    assert len(pts) == 6


def test_teton_in_jh_and_suppressed_skipped():
    pts, _ = _parse("outflow", "2122", counties={"56039": "Teton County"})
    assert [(p.indicator_id, p.value) for p in pts] == [("migration_outflow_returns", 1193), ("migration_outflow_individuals", 1807),
                                                         ("migration_outflow_agi", 299494)]   # trailing -1 summary row is skipped


def test_suppressed_summary_row_is_skipped():
    # a summary row with -1 values yields nothing (see the trailing Teton row in countyoutflow2122.csv, 3-digit padded)
    pts, _ = _parse("outflow", "2122", counties={"56039": "Teton County"})
    assert all(p.value > 0 for p in pts)


def test_bad_header_fails_loudly(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(ValueError, match="header"):
        list(IrsMigrationSource().parse(doc_from_file(f, meta={"direction": "inflow", "pair": "1112"}), FakeCtx(source_config=CFG)))


def test_next_pair():
    assert next_pair("2122") == "2223" and next_pair("9900") == "0001"


class _Resp:
    def __init__(self, code): self.status_code = code
    def raise_for_status(self): assert self.status_code < 400


class _Client:
    def __init__(self, last): self.last, self.urls = last, []
    def head(self, url):
        self.urls.append(url)
        return _Resp(200 if int(url[-8:-4]) <= self.last else 404)
    def close(self): pass


def test_year_pair_probing_and_incremental_window(monkeypatch):
    src = IrsMigrationSource()
    pairs = src.year_pairs(FakeCtx(source_config=CFG), client=_Client(2223))
    assert pairs[0] == "1112" and pairs[-1] == "2223" and len(pairs) == 12
    monkeypatch.setattr(IrsMigrationSource, "year_pairs", lambda self, ctx, client=None: pairs)
    full = src.fetch_specs(FakeCtx(source_config=CFG, backfill=True))
    inc = src.fetch_specs(FakeCtx(source_config=CFG, backfill=False))
    assert len(full) == 24 and [s.key for s in inc] == ["inflow_2122", "outflow_2122", "inflow_2223", "outflow_2223"]
    assert inc[0].url == "https://irs.test/countyinflow2122.csv"

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import ClassVar

import pytest

from argusvue.ingest import jh_airport
from argusvue.ingest.jh_airport import JhAirportSource, find_totals, parse_index
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "jh_airport"
CFG = {"index_page": "https://www.jacksonholeairport.com/airport-board/activity-reports/", "geo": "county:56039",
       "first_period": "2018-01", "incremental_months": 3}


def _ctx(**kw):
    return FakeCtx(region_id="jh", counties={"56039": "Teton County"}, source_config=CFG, **kw)


def test_parse_sep_2026_pdf():
    doc = doc_from_file(FIX / "2026-September.pdf", key="activity_202609",
                        meta={"period": "2026-09-01", "title": "September 2026 activity report"})
    pts = list(JhAirportSource().parse(doc, _ctx()))
    assert len(pts) == 1
    p = pts[0]
    assert (p.indicator_id, p.geo_key, p.period, p.value) == ("airport_enplanements", "county:56039", date(2026, 9, 1), 72065)
    assert p.excerpt.startswith("TOTALS 72,065 67,452")
    assert doc.text()[p.excerpt_start:p.excerpt_end] == p.excerpt
    assert p.attrs["prior_year_month"] == 67452
    assert "THIS CURRENT MONTH" in p.attrs["column"]


def test_header_month_mismatch_fails_loudly():
    doc = doc_from_file(FIX / "2026-September.pdf", key="x", meta={"period": "2026-08-01"})
    with pytest.raises(ValueError, match="header says"):
        list(JhAirportSource().parse(doc, _ctx()))


def test_find_totals_variants():
    text = "hdr\nALASKA 1 2 3 4\nTOTAL 1,234 1,000 5,000 4,000 9 9 9 9 1 1 1 1\nPERCENT CHANGE 1%"
    s, e, nums, line = find_totals(text)
    assert nums[:2] == [1234, 1000] and text[s:e] == line
    assert find_totals("nothing here") is None


def test_parse_index_links():
    html = (FIX / "index_sample.html").read_text()
    pdfs = parse_index(html, CFG["index_page"])
    assert pdfs[date(2026, 9, 1)].endswith("/2026/10/2026-September.pdf")
    assert pdfs[date(2018, 1, 1)].endswith("2018-january.pdf")
    assert date(2025, 12, 1) in pdfs and date(2026, 1, 1) in pdfs
    assert all(d.day == 1 for d in pdfs)
    assert len(pdfs) == 4  # load factor and tower report links are not monthly reports


class _Resp:
    status_code = 200
    url = CFG["index_page"]
    headers: ClassVar[dict] = {}
    text = (FIX / "index_sample.html").read_text()


class _Client:
    def __init__(self, *a, **k): ...
    def __enter__(self): return self
    def __exit__(self, *a): ...
    def get(self, url): return _Resp()


def test_fetch_specs_backfill_and_incremental(monkeypatch):
    monkeypatch.setattr(jh_airport.httpx, "Client", _Client)
    monkeypatch.setattr(jh_airport.time, "sleep", lambda s: None)
    full = JhAirportSource().fetch_specs(_ctx(backfill=True))
    assert [s.key for s in full] == ["activity_201801", "activity_202512", "activity_202601", "activity_202609"]
    assert full[0].meta["period"] == "2018-01-01" and full[0].suffix == ".pdf"
    inc = JhAirportSource().fetch_specs(_ctx(backfill=False))
    assert [s.key for s in inc] == [s.key for s in full[-3:]]

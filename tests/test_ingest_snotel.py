from datetime import date
from pathlib import Path

from argusvue.ingest.snotel import SnotelSource
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "snotel"
META = {"triplet": "689:WY:SNTL", "indicator": "snotel_swe_phillips_bench", "lat": 43.51687, "lon": -110.91258,
        "name": "Phillips Bench"}
CONTAINING = {(43.5169, -110.9126, "county"): "county:56039"}


def _points(today):
    ctx = FakeCtx(region_id="jh", counties={"56039": "Teton County"}, containing=CONTAINING, today=today)
    doc = doc_from_file(FIX / "data_689.json", key=META["indicator"], meta=META)
    return doc, list(SnotelSource().parse(doc, ctx))


def test_end_of_month_values_and_excerpts():
    doc, pts = _points(date(2026, 11, 5))
    by = {p.period: p for p in pts}
    assert by[date(2025, 3, 1)].value == 26.3 and by[date(2025, 3, 1)].attrs == {"station": "689:WY:SNTL", "obs_date": "2025-03-31"}
    assert by[date(2025, 4, 1)].attrs["obs_date"] == "2025-04-30"
    assert date(2025, 5, 1) not in by  # last obs 2025-05-02 is >3 days before month end
    text = doc.text()
    for p in pts:
        assert p.geo_key == "county:56039" and p.indicator_id == META["indicator"]
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
        assert p.excerpt == f'{{"date":"{p.attrs["obs_date"]}","value":{p.value}}}'


def test_incomplete_current_month_skipped():
    _, pts = _points(date(2025, 4, 20))
    assert [p.period for p in pts] == [date(2025, 3, 1)]


def test_month_complete_on_last_day():
    _, pts = _points(date(2025, 4, 30))
    assert [p.period for p in pts] == [date(2025, 3, 1), date(2025, 4, 1)]


def test_station_outside_gazetteer_yields_nothing():
    ctx = FakeCtx(region_id="jh", counties={"56039": "Teton County"}, containing={}, today=date(2026, 11, 5))
    doc = doc_from_file(FIX / "data_689.json", meta=META)
    assert list(SnotelSource().parse(doc, ctx)) == []

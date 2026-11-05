from datetime import date
from pathlib import Path

from argusvue.ingest.freddie_mac import PmmsSource
from tests.helpers import FakeCtx, doc_from_file

FX = Path(__file__).parent / "fixtures" / "freddie_mac" / "pmms_sample.csv"


def test_pmms_points():
    ctx = FakeCtx(source_config={"url": "x"})
    doc = doc_from_file(FX, key="pmms_history")
    pts = list(PmmsSource().parse(doc, ctx))
    assert len(pts) == 5                      # 3 early + 2 recent rows; the 2009 row has a blank pmms30
    assert {p.indicator_id for p in pts} == {"mortgage_rate_30y"} and {p.geo_key for p in pts} == {"country:US"}
    assert (pts[0].period, pts[0].value) == (date(1971, 4, 2), 7.33)
    assert (pts[-1].period, pts[-1].value) == (date(2026, 10, 29), 7.24)
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
    assert pts[-1].excerpt == "10/29/2026,7.24,,6.6,,,,,"


def test_pmms_fetch_spec():
    specs = PmmsSource().fetch_specs(FakeCtx(source_config={"url": "https://x.test/p.csv"}))
    assert [(s.url, s.suffix) for s in specs] == [("https://x.test/p.csv", ".csv")]

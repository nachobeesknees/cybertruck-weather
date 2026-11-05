from datetime import date
from pathlib import Path

from argusvue.ingest.nps_stats import NpsStatsSource
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "nps_stats" / "grte_2024.json"
CFG = {"base": "https://irmaservices.nps.gov/v3/rest/stats/visitation", "unit_code": "GRTE", "geo": "county:56039",
       "start_year": 1979}


def _ctx(**kw):
    return FakeCtx(region_id="jh", counties={"56039": "Teton County"}, source_config=CFG, today=date(2026, 11, 5), **kw)


def test_parse_monthly_records():
    doc = doc_from_file(FIX, key="GRTE")
    pts = list(NpsStatsSource().parse(doc, _ctx()))
    assert len(pts) == 12
    assert [p.period for p in pts] == [date(2024, m, 1) for m in range(1, 13)]
    assert pts[0].value == 51698 and pts[4].value == 320307
    text = doc.text()
    for p in pts:
        assert p.indicator_id == "grte_rec_visits" and p.geo_key == "county:56039"
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
        assert str(p.value) in p.excerpt


def test_skips_missing_values():
    import json
    import tempfile
    recs = [{"Month": 1, "NonRecreationVisitors": 1, "RecreationVisitors": None, "UnitCode": "GRTE", "Year": 2025},
            {"Month": 2, "NonRecreationVisitors": 1, "RecreationVisitors": 0, "UnitCode": "GRTE", "Year": 2025}]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.json"
        p.write_text(json.dumps(recs, separators=(",", ":")))
        pts = list(NpsStatsSource().parse(doc_from_file(p), _ctx()))
    assert [(x.period, x.value) for x in pts] == [(date(2025, 2, 1), 0)]


def test_fetch_specs_windows():
    spec, = NpsStatsSource().fetch_specs(_ctx(backfill=True))
    assert "startYear=1979" in spec.url and "endYear=2026" in spec.url and "endMonth=12" in spec.url
    assert spec.headers == {"Accept": "application/json"} and spec.suffix == ".json"
    spec, = NpsStatsSource().fetch_specs(_ctx(backfill=False))
    assert "startYear=2025" in spec.url

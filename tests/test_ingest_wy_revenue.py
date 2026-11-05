"""Wyoming DOR tax distribution reports: index link scraping, Total Distribution rows, Teton lodging."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

from argusvue.ingest import wy_revenue
from argusvue.ingest.wy_revenue import WyRevenueSource, parse_index_links
from tests.helpers import FakeCtx, doc_from_file

FIX = Path(__file__).parent / "fixtures" / "wy_revenue"
CFG = {
    "index_pages": {"total": "https://revenue.example/total", "su": "https://revenue.example/su"},
    "drive_download": "https://drive.example/download?id={id}",
    "entities": {"county": ["TETON", "LINCOLN"], "town": ["JACKSON", "ALPINE", "AFTON"]},
}
TETON, LINCOLN = "county:56039", "county:56023"
JACKSON, ALPINE, AFTON = "place:5640120", "place:5601695", "place:5600245"


def ctx(**kw) -> FakeCtx:
    return FakeCtx(region_id="jh", cbsa=None, counties={"56039": "Teton County", "56023": "Lincoln County"},
                   places={"5640120": "Jackson", "5601695": "Alpine", "5600245": "Afton"},
                   source_config=CFG, today=date(2026, 11, 5), **kw)


def total_points(name: str, fy: int, c=None):
    doc = doc_from_file(FIX / name, key=f"total_FY{fy}", meta={"report": "total", "fiscal_year": fy})
    return doc, list(WyRevenueSource().parse(doc, c or ctx()))


def by(points, geo):
    return {p.period: p for p in points if p.geo_key == geo}


# ---- index scraping and fetch specs ---------------------------------------------------------
def test_parse_index_links_handles_both_link_forms():
    links = parse_index_links((FIX / "index_total.html").read_text())
    got = {(ln.report, ln.fiscal_year, ln.drive_id) for ln in links}
    assert ("total", 2027, "1wVbOzUt1_gDssc1CZGgwGgh2BnQlNep2") in got
    assert ("total", 2025, "1L-wpkXkQrTuLtmXAcdiFvaEHMbVIXI9n") in got
    assert ("total", 2020, "1J0QOoY6y6Na2B3g9EmOZOwAkcYLgb8ck") in got      # open?id= link, "FY 2020" label
    assert ("bytype", 2003, "1c2FU85sVv3Cxem9MBmxhi003L1YSoJii") in got
    assert not any(ln.drive_id == "1dpjKNgq6jTqT09DQqmCNzyr3btLTkRI2" for ln in links)  # no FY label: ignored


def test_specs_backfill_vs_incremental_and_dedup():
    links = parse_index_links((FIX / "index_total.html").read_text())
    tmpl = CFG["drive_download"]
    back = WyRevenueSource.specs_from_links(links, tmpl, date(2026, 11, 5), True)
    assert [s.key for s in back] == ["total_FY2027", "total_FY2025", "total_FY2020", "bytype_FY2003"]
    assert len({s.url for s in back}) == len(back)                      # duplicate Drive id fetched once
    s = back[1]
    assert s.url == "https://drive.example/download?id=1L-wpkXkQrTuLtmXAcdiFvaEHMbVIXI9n"
    assert s.suffix == ".pdf" and s.meta == {"report": "total", "fiscal_year": 2025,
                                              "drive_id": "1L-wpkXkQrTuLtmXAcdiFvaEHMbVIXI9n"}
    inc = WyRevenueSource.specs_from_links(links, tmpl, date(2026, 11, 5), False)
    assert [s.key for s in inc] == ["total_FY2027"]                      # FY2027 + FY2026 only
    assert current_fy(date(2026, 6, 30)) == 2026 and current_fy(date(2026, 7, 1)) == 2027


def current_fy(d):
    return wy_revenue.current_fiscal_year(d)


def test_fetch_specs_scrapes_both_index_pages(monkeypatch):
    html = (FIX / "index_total.html").read_text()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["user-agent"]))
        return httpx.Response(200, text=html)

    real = httpx.Client
    monkeypatch.setattr(wy_revenue.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    sleeps = []
    monkeypatch.setattr(wy_revenue.time, "sleep", sleeps.append)
    specs = WyRevenueSource().fetch_specs(ctx(backfill=True))
    assert [u for u, _ in seen] == ["https://revenue.example/total", "https://revenue.example/su"]
    assert all("argusvue" in ua for _, ua in seen) and sleeps == [5.0]
    assert len(specs) == 4                                               # same links on both pages -> deduped


# ---- Total Distribution ---------------------------------------------------------------------
def test_total_fy2025_known_values():
    _, pts = total_points("total_FY2025.pdf", 2025)
    assert len(pts) == 5 * 12
    assert {p.indicator_id for p in pts} == {"sales_use_tax_distribution"}
    assert {p.geo_key for p in pts} == {TETON, LINCOLN, JACKSON, ALPINE, AFTON}
    teton = by(pts, TETON)
    assert sorted(teton) == [date(2024, m, 1) for m in range(7, 13)] + [date(2025, m, 1) for m in range(1, 7)]
    assert teton[date(2024, 7, 1)].value == Decimal("8081119.54")
    assert sum(p.value for p in teton.values()) == Decimal("68311295.35")
    assert sum(p.value for p in by(pts, LINCOLN).values()) == Decimal("8762645.85")
    assert by(pts, JACKSON)[date(2024, 7, 1)].value == Decimal("2960282.44")
    assert by(pts, AFTON)[date(2025, 6, 1)].value == Decimal("130045.01")
    assert by(pts, ALPINE)[date(2025, 1, 1)].value == Decimal("92937.21")


def test_total_excerpts_are_verbatim_and_contain_label_and_amount():
    doc, pts = total_points("total_FY2025.pdf", 2025)
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
        assert p.attrs["entity"] in p.excerpt
        assert p.attrs["amount_text"] in p.excerpt
        assert f"{p.value:,.2f}" == p.attrs["amount_text"]
    teton = by(pts, TETON)[date(2024, 7, 1)]
    assert teton.excerpt.startswith("TETON 8,081,119.54 7,787,044.43")
    assert "TETON VILLAGE" not in teton.excerpt


def test_total_in_progress_year_skips_future_months():
    _, pts = total_points("total_FY2027_partial.pdf", 2027)
    assert len(pts) == 5 * 4
    assert sorted(by(pts, TETON)) == [date(2026, m, 1) for m in (7, 8, 9, 10)]
    assert by(pts, TETON)[date(2026, 10, 1)].value == Decimal("16540015.61")


def test_total_skips_geo_not_in_gazetteer():
    c = FakeCtx(region_id="jh", cbsa=None, counties={"56039": "Teton County"}, places={"5640120": "Jackson"},
                source_config=CFG)
    _, pts = total_points("total_FY2025.pdf", 2025, c)
    assert {p.geo_key for p in pts} == {TETON, JACKSON}


HEADER = ("Name 07/31/2024 08/30/2024 Grand Total\n")


def _txt(tmp_path, body: str):
    p = tmp_path / "t.txt"
    p.write_text(HEADER + body)
    return doc_from_file(p, key="total_FY2025", meta={"report": "total", "fiscal_year": 2025})


def test_total_row_must_sum_to_grand_total(tmp_path):
    good = _txt(tmp_path, "1. Counties ALBANY 1.00 2.00 3.00\nTETON 10.00 20.00 30.00\n")
    assert [p.value for p in WyRevenueSource().parse(good, ctx())] == [Decimal("10.00"), Decimal("20.00")]
    bad = _txt(tmp_path, "1. Counties ALBANY 1.00 2.00 3.00\nTETON 10.00 20.00 31.00\n")       # OCR-style damage
    assert list(WyRevenueSource().parse(bad, ctx())) == []
    short = _txt(tmp_path, "1. Counties ALBANY 1.00 2.00 3.00\nTETON 10.00 30.00\n")             # missing cell
    assert list(WyRevenueSource().parse(short, ctx())) == []


def test_total_section_matters_and_ocr_dash_label(tmp_path):
    # TETON under Towns is not the county; "TETON --" (OCR noise) under Counties is
    towns = _txt(tmp_path, "3. Towns TETON 10.00 20.00 30.00\n")
    assert list(WyRevenueSource().parse(towns, ctx())) == []
    noisy = _txt(tmp_path, "1. Counties TETON -- 10.00 20.00 30.00\n")
    assert len(list(WyRevenueSource().parse(noisy, ctx()))) == 2


def test_total_year_without_text_layer_is_skipped(tmp_path):
    p = tmp_path / "scan.txt"
    p.write_text("Wyoming Department of Revenue ... Page 1 of 6")
    doc = doc_from_file(p, key="total_FY2021", meta={"report": "total", "fiscal_year": 2021})
    assert list(WyRevenueSource().parse(doc, ctx())) == []


def test_total_july_first_date_belongs_to_previous_month(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("Name 04/30/2004 05/28/2004 07/01/2004 Grand Total\n1. Counties TETON 1.00 2.00 3.00 6.00\n")
    doc = doc_from_file(p, key="total_FY2004", meta={"report": "total"})
    pts = list(WyRevenueSource().parse(doc, ctx()))
    assert [p.period for p in pts] == [date(2004, 4, 1), date(2004, 5, 1), date(2004, 6, 1)]


# ---- Teton lodging (by tax type) --------------------------------------------------------------
def lodging(name: str, fy: int):
    doc = doc_from_file(FIX / name, key=f"bytype_FY{fy}", meta={"report": "bytype", "fiscal_year": fy})
    return doc, list(WyRevenueSource().parse(doc, ctx()))


def test_lodging_fy2025_entity_share_of_state_lodging():
    doc, pts = lodging("bytype_FY2025_teton.pdf", 2025)
    assert len(pts) == 12 and {p.indicator_id for p in pts} == {"lodging_tax_distribution"}
    assert {p.geo_key for p in pts} == {TETON}
    v = {p.period: p for p in pts}
    assert v[date(2024, 7, 1)].value == Decimal("1700937.77")
    assert v[date(2025, 6, 1)].value == Decimal("491757.72")
    assert sum(p.value for p in pts) == Decimal("11469283.90")      # 11,472,752.55 share less 3,468.65 option adjustments
    text = doc.text()
    for p in pts:
        assert text[p.excerpt_start:p.excerpt_end] == p.excerpt
        assert "Sales Tax" in p.excerpt
        assert all(t in p.excerpt for t in p.attrs["columns"].values())
        assert p.value == sum(Decimal(t.replace(",", "").replace("(", "-").replace(")", ""))
                              for t in p.attrs["columns"].values())
    # March 2025 also has a (2,020.30) Lodging Option Tax adjustment, so that month is option + share
    mar = v[date(2025, 3, 1)]
    assert mar.attrs["formula"] == "lodging_option_tax + entity_share_of_state_lodging"
    assert mar.value == Decimal("805760.00") - Decimal("2020.30")
    assert "formula" not in v[date(2024, 7, 1)].attrs


def test_lodging_fy2024_transition_year_sums_option_and_share():
    doc, pts = lodging("bytype_FY2024_teton.pdf", 2024)
    v = {p.period: p for p in pts}
    assert len(pts) == 12
    assert v[date(2023, 7, 1)].value == Decimal("1243012.01")            # option tax only
    assert v[date(2024, 6, 1)].value == Decimal("472152.22")             # state lodging share only
    jan = v[date(2024, 1, 1)]                                            # both columns in one row
    assert jan.value == Decimal("671823.59") + Decimal("1133.45")
    assert set(jan.attrs["columns"]) == {"Lodging Option Tax", "Entity Share of State Lodging"}
    assert jan.attrs["formula"] == "lodging_option_tax + entity_share_of_state_lodging"
    for p in pts:
        assert doc.text()[p.excerpt_start:p.excerpt_end] == p.excerpt


def test_lodging_total_report_does_not_produce_lodging():
    _, pts = total_points("total_FY2025.pdf", 2025)
    assert all(p.indicator_id != "lodging_tax_distribution" for p in pts)

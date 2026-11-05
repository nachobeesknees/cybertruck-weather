"""Wyoming Department of Revenue monthly tax distribution reports (PDF, one per fiscal year).

Two report types are published on Google-Sites index pages, each linking Google Drive PDFs:

* "Total Distribution by Counties, Cities and Towns": one row per entity, 12 monthly columns
  (Jul..Jun of the fiscal year) plus a Grand Total. Gives `sales_use_tax_distribution`
  (the total of all state-administered sales/use/lodging distributions received by the entity).
* "SU Tax Report of Distribution Amounts by Entity by Tax Type": a very wide table per entity with
  one Sales Tax row (and one Use Tax row) per month and one column per tax type. We read the
  TETON County block and take the "Entity Share of State Lodging" column (Sales Tax rows) as
  `lodging_tax_distribution`.

Fiscal year FYyyyy runs July (yyyy-1) to June yyyy. Periods are the *month* a column belongs to by
position (the 12th column of a year is dated 07/01 in some years, so dates are not trusted blindly).

Reliability rules (nothing guessed):
* Total report: a row is only used when its amount count equals the header's column count and the
  monthly values add up to the Grand Total (catches OCR damage and merged/blank cells).
* Image-only years (no text layer) yield no points; they are logged and skipped.
"""

from __future__ import annotations

import html
import itertools
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource
from argusvue.settings import USER_AGENT

log = logging.getLogger(__name__)

AMT = r"\(?-?\d[\d,]*\.\d{2}\)?|\(?-?\.\d{2}\)?"
AMT_RE = re.compile(AMT)
DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
LINK_RE = re.compile(
    r'<a\b[^>]*href="https://drive\.google\.com/(?:file/d/([\w-]+)|open\?id=([\w-]+))[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
FY_RE = re.compile(r"FY\s*(\d{4})", re.IGNORECASE)
SECTION_RE = re.compile(r"^(\d+)\.\s+(\w+)\s+")
SECTION_NAMES = {"county": "Counties", "town": "Towns"}
ENTITY_GEO = {  # (section kind, label) -> ('county'|'place', name, state)
    ("county", "TETON"): ("county", "Teton County"),
    ("county", "LINCOLN"): ("county", "Lincoln County"),
    ("town", "JACKSON"): ("place", "Jackson"),
    ("town", "ALPINE"): ("place", "Alpine"),
    ("town", "AFTON"): ("place", "Afton"),
}
# Scanned (image-only, 50-70 MB, no text layer) by-tax-type reports: nothing to parse without OCR, so we do
# not download them. Re-check if DOR republishes these years (see docs/DECISIONS.md).
INDEX_DELAY_S = 5.0  # politeness gap between the two index-page requests (sources.yaml request_delay_s)
IMAGE_ONLY = {("bytype", 2021), ("bytype", 2022), ("bytype", 2023)}
REPORT_PATTERNS = (("total", re.compile(r"Total Distribution", re.IGNORECASE)), ("bytype", re.compile(r"SU Tax Report", re.IGNORECASE)))


@dataclass(frozen=True)
class DriveLink:
    drive_id: str
    fiscal_year: int
    report: str


def parse_index_links(page_html: str) -> list[DriveLink]:
    """Drive file links with a fiscal-year label and a recognised report title."""
    out: list[DriveLink] = []
    for m in LINK_RE.finditer(page_html):
        drive_id = m.group(1) or m.group(2)
        label = html.unescape(re.sub(r"<[^>]+>", "", m.group(3)))
        fy = FY_RE.search(label)
        report = next((name for name, pat in REPORT_PATTERNS if pat.search(label)), None)
        if fy and report:
            out.append(DriveLink(drive_id, int(fy.group(1)), report))
    return out


def current_fiscal_year(today: date) -> int:
    return today.year + 1 if today.month >= 7 else today.year


def _amount(tok: str) -> Decimal:
    neg = tok.startswith(("(", "-"))
    d = Decimal(tok.strip("()-").replace(",", "") or "0")
    return -d if neg else d


def _add_months(y: int, m: int, n: int) -> tuple[int, int]:
    k = y * 12 + (m - 1) + n
    return k // 12, k % 12 + 1


def _header_months(line: str) -> tuple[list[date], bool] | None:
    """A header line is dates only (optionally led by 'Name' and trailed by 'Grand Total'/'Totals').
    Returns (periods by position, has_total_column) or None when it is not a header or the dates are not
    consecutive months (a 07/01 date counts as the previous month)."""
    rest = DATE_RE.sub("", line)
    rest = re.sub(r"\b(Name|Grand Total|Totals?)\b", "", rest).strip()
    dates = DATE_RE.findall(line)
    if not dates or rest:
        return None
    months = []
    for mm, dd, yy in dates:
        y, m = int(yy), int(mm)
        if int(dd) <= 3:
            y, m = _add_months(y, m, -1)
        months.append((y, m))
    for a, b in itertools.pairwise(months):
        if _add_months(*a, 1) != b:
            return None
    has_total = bool(re.search(r"Total", line))
    return [date(y, m, 1) for y, m in months], has_total


class WyRevenueSource(StructuredSource):
    slug = "wy_revenue"

    # ---- discovery ---------------------------------------------------------------------------
    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        pages: dict[str, str] = cfg["index_pages"]
        template = cfg["drive_download"]
        links: list[DriveLink] = []
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True) as client:
            for i, url in enumerate(pages.values()):
                if i:
                    time.sleep(INDEX_DELAY_S)
                r = client.get(url)
                r.raise_for_status()
                links.extend(parse_index_links(r.text))
        return self.specs_from_links(links, template, ctx.today, ctx.backfill)

    @staticmethod
    def specs_from_links(links: Iterable[DriveLink], template: str, today: date, backfill: bool) -> list[FetchSpec]:
        cur = current_fiscal_year(today)
        seen: set[str] = set()
        specs: list[FetchSpec] = []
        for ln in sorted(links, key=lambda x: (-x.fiscal_year, x.report)):
            if not backfill and ln.fiscal_year < cur - 1:
                continue
            if ln.drive_id in seen or (ln.report, ln.fiscal_year) in IMAGE_ONLY:
                continue
            seen.add(ln.drive_id)
            specs.append(FetchSpec(
                url=template.format(id=ln.drive_id), key=f"{ln.report}_FY{ln.fiscal_year}", suffix=".pdf",
                title=f"Wyoming DOR {'Total Distribution' if ln.report == 'total' else 'SU Tax Report by Tax Type'} FY{ln.fiscal_year}",
                meta={"report": ln.report, "fiscal_year": ln.fiscal_year, "drive_id": ln.drive_id}))
        return specs

    # ---- parsing -----------------------------------------------------------------------------
    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        report = doc.meta.get("report") or doc.key.split("_")[0]
        if report == "total":
            yield from self._parse_total(doc, ctx)
        elif report == "bytype":
            yield from self._parse_bytype(doc, ctx)
        else:
            raise ValueError(f"unknown report type {report!r} for {doc.key}")

    # -- Total Distribution --------------------------------------------------------------------
    def _parse_total(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        text = doc.text()
        if not text.strip() or not DATE_RE.search(text):
            log.warning("wy_revenue %s: no text layer (image-only PDF); skipped", doc.key)
            return
        want = {(kind, label) for kind, labels in (ctx.source_config.get("entities") or {}).items() for label in labels}
        header: tuple[list[date], bool] | None = None
        section: str | None = None
        pos = 0
        found: set[tuple[str, str]] = set()
        for line in text.split("\n"):
            line_start, pos = pos, pos + len(line) + 1
            h = _header_months(line)
            if h:
                header = h
                continue
            if header is None:
                continue
            m = SECTION_RE.match(line)
            label_start = 0
            if m:
                section = m.group(2)
                label_start = m.end()
            first = AMT_RE.search(line, label_start)
            if first is None:
                continue
            label = line[label_start:first.start()].strip().strip("-–—* ").strip()
            kind = next((k for k, v in SECTION_NAMES.items() if v == section), None)
            if kind is None or (kind, label) not in want or (kind, label) in found:
                continue
            gm = ENTITY_GEO.get((kind, label))
            if gm is None:
                continue
            periods, has_total = header
            toks = AMT_RE.findall(line[first.start():])
            if len(toks) != len(periods) + (1 if has_total else 0):
                log.warning("wy_revenue %s: %s %s has %d amounts, header has %d columns; row skipped",
                            doc.key, kind, label, len(toks), len(periods) + int(has_total))
                continue
            vals = [_amount(t) for t in toks]
            if has_total and abs(sum(vals[:-1]) - vals[-1]) > Decimal("0.05"):
                log.warning("wy_revenue %s: %s %s monthly values do not sum to the Grand Total; row skipped",
                            doc.key, kind, label)
                continue
            geo = (ctx.county_by_name(gm[1], "WY") if gm[0] == "county" else ctx.place_by_name(gm[1], "WY"))
            if geo is None:
                continue
            found.add((kind, label))
            ex_start = line_start + label_start
            ex_end = line_start + len(line)
            excerpt = text[ex_start:ex_end]
            for period, tok, val in zip(periods, toks[:len(periods)], vals[:len(periods)], strict=True):
                yield Point(
                    indicator_id="sales_use_tax_distribution", geo_key=geo, period=period, value=val,
                    excerpt=excerpt, excerpt_start=ex_start, excerpt_end=ex_end,
                    attrs={"entity": label, "section": section, "fiscal_year": doc.meta.get("fiscal_year"),
                           "amount_text": tok, "report": "total_distribution",
                           "meaning": "all state-administered sales, use and lodging tax distributions received by the entity"})

    # -- By Tax Type (Teton lodging) -------------------------------------------------------------
    def _parse_bytype(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        import pdfplumber

        text = doc.text()
        if not text.strip():
            log.warning("wy_revenue %s: no text layer; skipped", doc.key)
            return
        geo = ctx.county_by_name("Teton County", "WY")
        if geo is None:
            return
        pages = text.split("\f")
        offsets: list[int] = []
        o = 0
        for p in pages:
            offsets.append(o)
            o += len(p) + 1
        teton = [i for i, p in enumerate(pages)
                 if any(ln.strip() == "TETON County" for ln in p.split("\n")[:8])]
        if not teton:
            log.warning("wy_revenue %s: no TETON County block found", doc.key)
            return
        with pdfplumber.open(doc.blob_path) as pdf:
            for pi in teton:
                yield from self._teton_page_points(pdf.pages[pi], pages[pi], offsets[pi], geo, doc)

    def _teton_page_points(self, page, page_text: str, page_offset: int, geo: str, doc: FetchedDocument):
        """Points for one TETON County page of the by-tax-type report.

        Columns are located from the header words (layouts differ by year; the sparse table prints no
        placeholder for blank cells so text order alone cannot identify a column). Two lodging columns
        exist: "Lodging Option Tax" (Teton's local option tax, to ~Jan 2024; adjustments only afterwards)
        and "Entity Share of State Lodging" (from ~Jan 2024). The monthly value is the Sales Tax row's
        option + share (either may be blank); Use Tax rows carry no lodging. Each column sum is checked
        against the page's `Totals:` line before any point is emitted."""
        words = page.extract_words()
        row_dates = [w for w in words if w["x0"] < 100 and DATE_RE.fullmatch(w["text"])]
        if not row_dates:
            return
        ft = min(w["top"] for w in row_dates)
        def mid(w) -> float:
            return (w["x0"] + w["x1"]) / 2

        option_cx = share_cx = state_tax_cx = alloc_cx = None
        for w in words:
            if w["top"] >= ft - 2:
                continue
            if w["text"] == "Lodging":
                if any(v["text"] == "State" and 0 < w["top"] - v["top"] < 20 and abs(mid(v) - mid(w)) < 20 for v in words):
                    share_cx = mid(w)
                elif any(v["text"] == "Option" and 0 < v["top"] - w["top"] < 20 and abs(mid(v) - mid(w)) < 15 for v in words):
                    option_cx = mid(w)
            elif w["text"].strip("()") == "Guaranteed":
                alloc_cx = mid(w)
            elif w["text"] == "Tax" and w["x0"] > page.width * 0.6:
                left = [v for v in words if v["text"] == "State" and abs(v["top"] - w["top"]) < 3 and 0 <= w["x0"] - v["x1"] < 8]
                if left:
                    state_tax_cx = (left[0]["x0"] + w["x1"]) / 2
        if option_cx is None and share_cx is None:
            log.warning("wy_revenue %s: no lodging column header found on the Teton page; skipped", doc.key)
            return
        tl = re.search(r"^Totals:.*$", page_text, re.MULTILINE)
        if tl is None:
            log.warning("wy_revenue %s: Teton Totals line not found; skipped", doc.key)
            return
        lm = re.search(r"Lodging:\s*(-?[\d,]*\.\d{2})?", tl.group(0))
        sm = re.search(r"State(?: Tax)?:\s*(-?[\d,]*\.\d{2})", tl.group(0))
        tot_lodging = _amount(lm.group(1)) if lm and lm.group(1) else Decimal(0)  # blank = no lodging column data
        tot_state = _amount(sm.group(1)) if sm else None
        totals_top = min((w["top"] for w in words if w["text"] == "Totals:"), default=1e9)

        def col_of(w) -> str | None:
            cx = mid(w)
            hits = [name for name, c in (("option", option_cx), ("share", share_cx), ("state_tax", state_tax_cx),
                                   ("alloc", alloc_cx))
                    if c is not None and abs(cx - c) <= 22]
            return hits[0] if len(hits) == 1 else None

        body = [w for w in words if ft - 2 <= w["top"] < totals_top - 1 and w["x0"] > 100 and re.fullmatch(AMT, w["text"])]
        sums = {"option": Decimal(0), "share": Decimal(0), "state_tax": Decimal(0), "alloc": Decimal(0)}
        for w in body:
            c = col_of(w)
            if c:
                sums[c] += _amount(w["text"])
        if abs(sums["option"] - tot_lodging) > Decimal("0.05"):
            log.warning("wy_revenue %s: Lodging Option Tax column sum %s != page total %s; Teton page skipped",
                        doc.key, sums["option"], tot_lodging)
            return
        if share_cx is not None:  # the "State:" total = state tax + state lodging + county allocation columns
            state_sum = sums["state_tax"] + sums["share"] + sums["alloc"]
            if state_tax_cx is None or alloc_cx is None or tot_state is None or abs(state_sum - tot_state) > Decimal("0.05"):
                log.warning("wy_revenue %s: State columns sum %s != page total %s; Teton page skipped",
                            doc.key, state_sum, tot_state)
                return

        lines: dict[int, list[dict]] = {}
        for w in sorted(words, key=lambda w: w["top"]):
            if w["top"] < ft - 2 or w["top"] >= totals_top - 1:
                continue
            key = next((k for k in lines if abs(k - w["top"]) < 3), None)
            lines.setdefault(round(w["top"]) if key is None else key, []).append(w)
        seen: set[date] = set()
        out: list[Point] = []
        for ws in lines.values():
            ws.sort(key=lambda w: w["x0"])
            toks = [w["text"] for w in ws]
            if len(toks) < 3 or not DATE_RE.fullmatch(toks[0]) or toks[1:3] != ["Sales", "Tax"]:
                continue
            mm, dd, yy = DATE_RE.fullmatch(toks[0]).groups()
            y, m = int(yy), int(mm)
            if int(dd) <= 3:
                y, m = _add_months(y, m, -1)
            period = date(y, m, 1)
            parts: list[tuple[str, str]] = []
            for w in ws[3:]:
                if re.fullmatch(AMT, w["text"]):
                    c = col_of(w)
                    if c in ("option", "share"):
                        parts.append((c, w["text"]))
            if not parts:
                continue  # both lodging cells blank: nothing published, so no point (not a zero)
            if period in seen or len({c for c, _ in parts}) != len(parts):
                log.warning("wy_revenue %s: ambiguous Teton lodging row %s; page skipped", doc.key, toks[0])
                return
            seen.add(period)
            line_re = re.compile(
                rf"^{re.escape(toks[0])} Sales Tax .*$", re.MULTILINE)
            cands = [mt for mt in line_re.finditer(page_text)
                     if all(re.search(rf"(?<![\d,.]){re.escape(t)}(?![\d,])", mt.group(0)) for _, t in parts)]
            if len(cands) != 1:
                log.warning("wy_revenue %s: no unique text line for Teton %s; page skipped", doc.key, toks[0])
                return
            line = cands[0]
            start = page_offset + line.start()
            value = sum((_amount(t) for _, t in parts), Decimal(0))
            attrs = {"entity": "TETON County", "tax_row": "Sales Tax", "fiscal_year": doc.meta.get("fiscal_year"),
                     "report": "su_tax_by_entity_by_tax_type",
                     "columns": {("Lodging Option Tax" if c == "option" else "Entity Share of State Lodging"): t
                                 for c, t in parts}}
            if len(parts) == 2:
                attrs["formula"] = "lodging_option_tax + entity_share_of_state_lodging"
            out.append(Point(indicator_id="lodging_tax_distribution", geo_key=geo, period=period, value=value,
                             excerpt=line.group(0), excerpt_start=start, excerpt_end=start + len(line.group(0)),
                             attrs=attrs))
        yield from out

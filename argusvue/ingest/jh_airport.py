"""Jackson Hole Airport board activity reports (monthly PDFs, by airline).

The index page links one PDF per month with link text 'YYYY Month'. Each PDF carries a table whose
first number group is PASSENGERS ENPLANED / THIS CURRENT MONTH; the TOTALS row is
`TOTALS <enp cur> <enp prev-yr> <enp cur YTD> <enp prev YTD> <deplaned x4> <landings x4>`.
Only the first number (current month, current year) is used.
"""

from __future__ import annotations

import calendar
import logging
import re
import time
from collections.abc import Iterable
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, StructuredSource
from argusvue.settings import USER_AGENT

log = logging.getLogger(__name__)

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
LINK_TEXT = re.compile(r"^\s*(\d{4})\s+([A-Za-z]+)\s*$")
HEADER = re.compile(r"^\s*([A-Za-z]+)\s+(\d{4})\s*$")
TOTAL_LINE = re.compile(r"^\s*TOTALS?\b\s*((?:[\d,]+\s*)+)$", re.IGNORECASE)
CRAWL_DELAY_S = 10.0


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def parse_index(html: str, base_url: str) -> dict[date, str]:
    """period (first of month) -> absolute PDF url, from links whose text is 'YYYY Month'."""
    p = _Links()
    p.feed(html)
    out: dict[date, str] = {}
    for href, text in p.links:
        m = LINK_TEXT.match(text)
        if not m or not href.lower().split("?")[0].endswith(".pdf"):
            continue
        month = MONTHS.get(m.group(2).lower())
        if month is None:
            continue
        out.setdefault(date(int(m.group(1)), month, 1), urljoin(base_url, href))
    return out


def find_totals(text: str) -> tuple[int, int, list[int], str] | None:
    """Locate the TOTALS row. Returns (start_offset, end_offset, numbers, line) or None."""
    pos = 0
    for raw in text.split("\n"):
        m = TOTAL_LINE.match(raw)
        if m:
            nums = [int(x.replace(",", "")) for x in m.group(1).split()]
            if len(nums) >= 4:
                return pos, pos + len(raw), nums, raw
        pos += len(raw) + 1
    return None


def report_month(text: str) -> date | None:
    """Month printed in the report header ('September 2026'), if present."""
    for raw in text.split("\n")[:8]:
        m = HEADER.match(raw)
        if m and m.group(1).lower() in MONTHS:
            return date(int(m.group(2)), MONTHS[m.group(1).lower()], 1)
    return None


class JhAirportSource(StructuredSource):
    slug = "jh_airport"

    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        cfg = ctx.source_config
        index = cfg["index_page"]
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as client:
            r = client.get(index)
            time.sleep(CRAWL_DELAY_S)  # Crawl-delay 10 before the runner's first request to this host
        if r.status_code != 200:
            raise RuntimeError(
                f"jh_airport: index page returned HTTP {r.status_code} "
                f"(cf-mitigated={r.headers.get('cf-mitigated')!r}); not attempting to bypass a bot challenge")
        pdfs = parse_index(r.text, str(r.url))
        if not pdfs:
            raise RuntimeError("jh_airport: no monthly report links found on the index page")
        first = date.fromisoformat(cfg["first_period"] + "-01")
        periods = sorted(p for p in pdfs if p >= first)
        if not ctx.backfill:
            periods = periods[-int(cfg.get("incremental_months", 3)):]
        return [FetchSpec(url=pdfs[p], key=f"activity_{p:%Y%m}", suffix=".pdf",
                          title=f"JAC board activity report {p:%B %Y}",
                          meta={"period": p.isoformat(), "title": f"{p:%B %Y} activity report"})
                for p in periods]

    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        period = date.fromisoformat(doc.meta["period"])
        text = doc.text()
        printed = report_month(text)
        if printed is not None and printed != period:
            raise ValueError(f"{doc.url}: report header says {printed:%B %Y} but link text said {period:%B %Y}")
        found = find_totals(text)
        if found is None:
            raise ValueError(f"{doc.url}: no TOTALS row found in report text")
        start, end, nums, line = found
        geo = ctx.source_config["geo"]
        yield Point(indicator_id="airport_enplanements", geo_key=geo, period=period, value=nums[0], excerpt=line,
                    excerpt_start=start, excerpt_end=end,
                    attrs={"column": "PASSENGERS ENPLANED / THIS CURRENT MONTH (first number of TOTALS row)",
                           "prior_year_month": nums[1], "header_month_verified": printed is not None})

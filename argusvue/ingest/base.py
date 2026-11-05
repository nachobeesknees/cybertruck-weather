"""Structured-source ingest interface.

A source declares what to fetch (`fetch_specs`) and how to turn a fetched document into indicator
points (`parse`). The runner (ingest/runner.py) does everything else: snapshot to BlobStore, document
dedup by content hash, region filtering, excerpt verification, claim + indicator_point upserts with
revision handling, job logging. Sources never touch the database directly except through `ctx`.
"""

from __future__ import annotations

import calendar
import gzip
import io
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import psycopg

from argusvue.geo.states import FIPS_BY_USPS

FREQUENCIES = ("daily", "weekly", "monthly", "quarterly", "annual")


@dataclass(frozen=True)
class FetchSpec:
    """One HTTP request whose response becomes one document."""

    url: str
    key: str = ""                       # logical name so parse() knows which file it has
    method: str = "GET"
    body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    suffix: str = ""                    # blob suffix, e.g. ".csv" or ".tsv.gz"
    title: str | None = None
    published_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchedDocument:
    id: int
    url: str
    key: str
    blob_path: Path
    fetched_at: datetime
    content_hash: str
    size: int
    mime: str | None
    meta: dict[str, Any]
    is_new: bool
    _text: str | None = None

    # -- text access ------------------------------------------------------------------------
    @property
    def is_pdf(self) -> bool:
        with self.blob_path.open("rb") as f:
            return f.read(5) == b"%PDF-"

    def open_bytes(self) -> io.BufferedReader | gzip.GzipFile:
        f = self.blob_path.open("rb")
        magic = f.peek(2)[:2]
        if magic == b"\x1f\x8b":
            return gzip.GzipFile(fileobj=f)
        return f

    def open_text(self, encoding: str = "utf-8") -> TextIO:
        if self.is_pdf:
            return io.StringIO(self.text())
        return io.TextIOWrapper(self.open_bytes(), encoding=encoding, errors="replace", newline="")

    def text(self, encoding: str = "utf-8") -> str:
        """Document text. For PDFs this is the pdfplumber extraction (pages joined by form feeds), and
        excerpt offsets index into it; for everything else it is the decoded file."""
        if self._text is None:
            if self.is_pdf:
                self._text = extract_pdf_text(self.blob_path)
            else:
                with self.open_text(encoding) as f:
                    self._text = f.read()
        return self._text

    def pdf_pages(self) -> list[str]:
        """Per-page extracted text (same text as `text()`, split on form feeds)."""
        return self.text().split("\f")

    def json(self) -> Any:
        return json.loads(self.text())

    def iter_lines(self, encoding: str = "utf-8") -> Iterator[tuple[int, str]]:
        """Yield (char_offset, line_without_newline). Offsets index the decoded text, so
        text[offset:offset+len(line)] == line."""
        pos = 0
        with self.open_text(encoding) as f:
            for raw in f:
                line = raw.rstrip("\r\n")
                yield pos, line
                pos += len(raw)

    def read_span(self, start: int, end: int, encoding: str = "utf-8") -> str:
        if self._text is not None:
            return self._text[start:end]
        with self.open_text(encoding) as f:
            f.read(start)
            return f.read(end - start)


def extract_pdf_text(path: Path) -> str:
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\f".join(pages)


@dataclass
class Point:
    indicator_id: str
    geo_key: str
    period: date
    value: float | int | Decimal
    excerpt: str
    excerpt_start: int | None = None
    excerpt_end: int | None = None
    valid_from: date | None = None      # defaults from the indicator's frequency
    valid_to: date | None = None
    unit: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


def period_bounds(frequency: str, period: date) -> tuple[date, date]:
    if frequency == "daily":
        return period, period
    if frequency == "weekly":
        return period - timedelta(days=6), period
    if frequency == "monthly":
        last = calendar.monthrange(period.year, period.month)[1]
        return period.replace(day=1), period.replace(day=last)
    if frequency == "quarterly":
        q_start_month = 3 * ((period.month - 1) // 3) + 1
        start = period.replace(month=q_start_month, day=1)
        end_month = q_start_month + 2
        return start, date(period.year, end_month, calendar.monthrange(period.year, end_month)[1])
    if frequency == "annual":
        return date(period.year, 1, 1), date(period.year, 12, 31)
    raise ValueError(f"unknown frequency {frequency!r}")


def month_start(year: int, month: int) -> date:
    return date(year, month, 1)


@dataclass(frozen=True)
class RegionInfo:
    """What a source needs to know about a region, read from the `regions` table at run time."""

    id: str
    cbsa: str
    counties: list[str]                 # all counties incl. commuter shed

    @property
    def states(self) -> list[str]:
        return sorted({c[:2] for c in self.counties})


class IngestContext:
    """Per-run context handed to sources: region, allowed geo keys, lookups into the gazetteer."""

    def __init__(self, conn: psycopg.Connection, region: RegionInfo | None, region_id: str,
                 backfill: bool, source_config: dict[str, Any]):
        self.conn = conn
        self.region = region
        self.region_id = region_id
        self.backfill = backfill
        self.source_config = source_config
        self.today = date.today()
        self._load_geo()

    @staticmethod
    def load_region(conn: psycopg.Connection, region_id: str) -> RegionInfo:
        with conn.cursor() as cur:
            cur.execute("SELECT id, cbsa_code, county_fips FROM regions WHERE id = %s", (region_id,))
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"region {region_id!r} is not loaded (run `argusvue geo load --region {region_id}`)")
        return RegionInfo(id=row["id"], cbsa=row["cbsa_code"], counties=list(row["county_fips"]))

    def _load_geo(self) -> None:
        with self.conn.cursor() as cur:
            if self.region is not None:
                cur.execute(
                    """SELECT geo_key, level::text AS level, code, name, attrs FROM geo_units
                       WHERE region_id = %s OR geo_key = 'country:US'
                          OR (level = 'state' AND code = ANY(%s))""",
                    (self.region_id, self.region.states),
                )
            else:
                cur.execute("SELECT geo_key, level::text AS level, code, name, attrs FROM geo_units WHERE level IN ('country','state','cbsa','county')")
            rows = cur.fetchall()
        self.geo_keys: set[str] = {r["geo_key"] for r in rows}
        self.by_level: dict[str, dict[str, str]] = {}
        self.names: dict[str, str] = {r["geo_key"]: r["name"] for r in rows}
        self._place_by_name: dict[tuple[str, str], str] = {}
        self._county_by_name: dict[tuple[str, str], str] = {}
        for r in rows:
            self.by_level.setdefault(r["level"], {})[r["code"]] = r["geo_key"]
            if r["level"] == "place":
                short = (r["attrs"] or {}).get("short_name") or r["name"]
                self._place_by_name[(r["code"][:2], _norm(short))] = r["geo_key"]
            elif r["level"] == "county":
                self._county_by_name[(r["code"][:2], _norm(r["name"]))] = r["geo_key"]

    # -- lookups ------------------------------------------------------------------------------
    def in_region(self, geo_key: str) -> bool:
        return geo_key in self.geo_keys

    def zcta(self, code: str) -> str | None:
        return self.by_level.get("zcta", {}).get(code.zfill(5))

    def county(self, fips: str) -> str | None:
        return self.by_level.get("county", {}).get(fips.zfill(5))

    def cbsa(self, code: str) -> str | None:
        return self.by_level.get("cbsa", {}).get(code)

    def place(self, code7: str) -> str | None:
        return self.by_level.get("place", {}).get(code7)

    def place_by_name(self, name: str, state_usps: str) -> str | None:
        """'Phoenix' + 'AZ' -> place:0455000 (matches the gazetteer short name, case-insensitive)."""
        fips = FIPS_BY_USPS.get(state_usps.upper())
        if not fips:
            return None
        return self._place_by_name.get((fips, _norm(name)))

    def county_by_name(self, name: str, state_usps: str) -> str | None:
        """'Maricopa County' + 'AZ' -> county:04013 (full name as in TIGER NAMELSAD)."""
        fips = FIPS_BY_USPS.get(state_usps.upper())
        if not fips:
            return None
        return self._county_by_name.get((fips, _norm(name)))

    @property
    def county_fips(self) -> list[str]:
        return self.region.counties if self.region else []

    @property
    def state_fips(self) -> list[str]:
        return self.region.states if self.region else []

    @property
    def cbsa_code(self) -> str | None:
        return self.region.cbsa if self.region else None

    def containing(self, lat: float, lon: float, level: str) -> str | None:
        """Gazetteer unit of `level` containing a point (e.g. the county of a SNOTEL station)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT geo_key FROM geo_containing(ST_SetSRID(ST_Point(%s, %s), 4326)) WHERE level = %s LIMIT 1",
                (lon, lat, level),
            )
            row = cur.fetchone()
        return row["geo_key"] if row else None


def _norm(s: str) -> str:
    return " ".join(s.lower().replace(",", " ").split())


class StructuredSource(ABC):
    """Implement one per structured source in ingest/<slug>.py and register it in config/sources.yaml."""

    slug: str = ""

    @abstractmethod
    def fetch_specs(self, ctx: IngestContext) -> list[FetchSpec]:
        """What to download for this run. Backfill vs incremental is `ctx.backfill`."""

    @abstractmethod
    def parse(self, doc: FetchedDocument, ctx: IngestContext) -> Iterable[Point]:
        """Turn one fetched document into indicator points. Yield only points whose geo_key is in
        `ctx.geo_keys` (the runner drops the rest and counts them). Every point's excerpt must be a
        verbatim span of the document text; give offsets when you have them."""

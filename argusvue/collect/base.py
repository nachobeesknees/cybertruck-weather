"""Collector interface (SPEC §6): run(objective, schema, geo_scope, budget) -> list[RawDocument].

Adapters: feed_monitor (RSS/Atom), page_monitor (index pages with link lists), search (Claude web
search), own_fetch (explicit URL). Every adapter fetches through `collect.fetcher` so robots.txt,
crawl delay and snapshots are handled in one place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Budget:
    max_items: int = 20          # documents to fetch per run
    max_searches: int = 3        # web search uses (search adapter)
    max_bytes: int = 15 << 20    # per document


@dataclass
class RawDocument:
    url: str
    fetched_at: datetime
    content: bytes
    mime: str | None
    text: str                    # extracted text (HTML -> trafilatura, PDF -> pdfplumber, else decoded)
    title: str | None = None
    published_at: datetime | None = None
    origin_key: str | None = None    # canonical origin (domain, wire service, press-release issuer)
    discovered_via: str | None = None  # feed URL, index page, or search objective
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MonitorConfig:
    """One monitor from config/monitors.yaml (synced into `sources` as kind feed|web_monitor|search|own_fetch)."""

    id: str
    region: str
    kind: str                    # feed | page | search | url
    target: str                  # feed URL, index page URL, search objective, or explicit URL
    cadence: str = "daily"       # hourly | daily | weekly
    tags: list[str] = field(default_factory=list)
    reliability: float = 0.5
    origin_key: str | None = None
    allow_patterns: list[str] = field(default_factory=list)   # regexes a discovered link must match
    deny_patterns: list[str] = field(default_factory=list)
    max_items: int = 20
    paywall: bool = False
    notes: str | None = None
    domains: list[str] = field(default_factory=list)          # search: allowed domains
    geo_scope: str | None = None
    keywords: list[str] = field(default_factory=list)         # feed/json: fetch an item only if title/summary matches one
    ai_bot_rule: bool = False                                  # host robots.txt names AI crawlers (not our UA); see DECISIONS
    json_items: str = ""                                       # json: key holding the item list ('' = root list, 'value' = OData)
    json_link_keys: list[str] = field(default_factory=list)   # json: item keys whose values are document URLs
    json_date_key: str | None = None                           # json: item date key (ISO)
    json_title_keys: list[str] = field(default_factory=list)
    max_age_days: int = 60                                     # json: ignore items older than this (and future placeholders)


class Collector(ABC):
    kind: str = ""

    @abstractmethod
    def run(self, objective: str, schema: dict | None, geo_scope: str | None, budget: Budget) -> list[RawDocument]:
        """Collect documents for an objective (a feed URL, a page, a query). Never raises for a single
        bad item; records per-item errors in `self.errors`."""

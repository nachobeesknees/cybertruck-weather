"""Search collector: Claude web search proposes URLs for an objective; our own fetcher gets the pages
(robots-checked) so every stored document is a verbatim snapshot with provenance."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import psycopg
from pydantic import BaseModel, Field

from argusvue import llm
from argusvue.collect.base import Budget, Collector, RawDocument
from argusvue.collect.fetcher import Fetcher, FetchError, RobotsDisallowed
from argusvue.store.blobs import BlobStore

log = logging.getLogger(__name__)

SCOUT_SYSTEM = """You are a research scout for a regional real-estate and economic intelligence system.
Given an objective and a geographic scope, use web search to find recent, primary, publicly accessible web pages
that report concrete facts (numbers, decisions, filings, announcements) relevant to the objective. Prefer official
sources (government, agencies, companies), then established local news. Never include paywalled subscriber content
or pages that require login. Return only pages you actually saw in search results. Do not summarise the facts
yourself; the pages will be fetched and read separately."""


class SearchHit(BaseModel):
    url: str
    title: str | None = None
    published: str | None = Field(default=None, description="YYYY-MM-DD if known")
    why_relevant: str = ""


class SearchFindings(BaseModel):
    hits: list[SearchHit] = Field(default_factory=list)
    notes: str = ""


class SearchCollector(Collector):
    kind = "search"

    def __init__(self, conn: psycopg.Connection, fetcher: Fetcher, *, seen: Callable[[str], bool], region_id: str | None,
                 domains: list[str] | None = None, store: BlobStore | None = None, client=None, since_days: int = 30):
        self.conn = conn
        self.fetcher = fetcher
        self.seen = seen
        self.region_id = region_id
        self.domains = domains or []
        self.store = store
        self.client = client
        self.since_days = since_days
        self.errors: list[str] = []
        self.run_id: int | None = None
        self.cost_usd = 0.0
        self.feed_unchanged = False

    def run(self, objective: str, schema: dict | None, geo_scope: str | None, budget: Budget) -> list[RawDocument]:
        user = (f"Objective: {objective}\nGeographic scope: {geo_scope or self.region_id}\n"
                f"Today: {date.today().isoformat()}. Prefer items from the last {self.since_days} days.\n"
                f"Return JSON matching this schema: {SearchFindings.model_json_schema()}\n"
                f"Give at most {budget.max_items} hits, best first.")
        res = llm.complete(self.conn, "scout", system=SCOUT_SYSTEM, user=user, schema=SearchFindings,
                           region_id=self.region_id, store=self.store, client=self.client,
                           tools=[llm.web_search_tool(max_uses=budget.max_searches, allowed_domains=self.domains or None)])
        self.run_id, self.cost_usd = res.run_id, res.cost_usd
        urls: list[str] = []
        for h in res.parsed.hits:
            if h.url not in urls:
                urls.append(h.url)
        for c in res.citations:  # URLs the model actually saw
            if c.get("url") and c["url"] not in urls:
                urls.append(c["url"])
        out: list[RawDocument] = []
        for url in urls:
            if self.seen(url):
                continue
            try:
                doc = self.fetcher.fetch(url, discovered_via=f"search:{objective}")
            except RobotsDisallowed as ex:
                self.errors.append(f"robots: {url}: {ex}")
                continue
            except FetchError as ex:
                self.errors.append(f"fetch: {url}: {ex}")
                continue
            doc.meta["scout_run_id"] = self.run_id
            out.append(doc)
            if len(out) >= budget.max_items:
                break
        return out

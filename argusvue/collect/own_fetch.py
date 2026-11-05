"""Explicit-URL collector: fetch one configured page/PDF every run. Unchanged content lands as an exact
duplicate, so the attention ledger still records the check."""

from __future__ import annotations

from argusvue.collect.base import Budget, Collector, RawDocument
from argusvue.collect.fetcher import Fetcher, FetchError


class UrlCollector(Collector):
    kind = "url"

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher
        self.errors: list[str] = []
        self.feed_unchanged = False

    def run(self, objective: str, schema: dict | None, geo_scope: str | None, budget: Budget) -> list[RawDocument]:
        try:
            return [self.fetcher.fetch(objective, discovered_via=f"url:{objective}")]
        except FetchError as ex:
            self.errors.append(f"fetch: {objective}: {ex}")
            return []

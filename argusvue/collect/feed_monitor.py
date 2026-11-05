"""RSS/Atom feed monitor: fetch the feed (robots-checked), fetch new entries, return RawDocuments."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

import feedparser

from argusvue.collect.base import Budget, Collector, RawDocument
from argusvue.collect.fetcher import Fetcher, FetchError, RobotsDisallowed

log = logging.getLogger(__name__)


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(time.mktime(st), tz=UTC)
            except (OverflowError, ValueError):
                return None
    return None


class FeedMonitor(Collector):
    kind = "feed"

    def __init__(self, fetcher: Fetcher, *, seen: Callable[[str], bool], allow: list[str] | None = None,
                 deny: list[str] | None = None, state: dict | None = None, keywords: list[str] | None = None):
        self.fetcher = fetcher
        self.seen = seen
        self.allow = [re.compile(p) for p in (allow or [])]
        self.deny = [re.compile(p) for p in (deny or [])]
        self.keywords = [k.lower() for k in (keywords or [])]
        self.state = state if state is not None else {}
        self.errors: list[str] = []
        self.entries_seen = 0
        self.feed_unchanged = False

    def _ok(self, url: str) -> bool:
        if self.allow and not any(p.search(url) for p in self.allow):
            return False
        return not any(p.search(url) for p in self.deny)

    def run(self, objective: str, schema: dict | None, geo_scope: str | None, budget: Budget) -> list[RawDocument]:
        content, _mime, _final = self.fetcher.get_bytes(objective)
        digest = hashlib.sha256(content).hexdigest()
        if self.state.get("feed_hash") == digest:
            self.feed_unchanged = True
            return []
        self.state["feed_hash"] = digest
        feed = feedparser.parse(content)
        if feed.bozo and not feed.entries:
            raise FetchError(f"feed {objective} did not parse: {feed.bozo_exception}")
        entries = sorted(feed.entries, key=lambda e: _entry_time(e) or datetime.min.replace(tzinfo=UTC), reverse=True)
        out: list[RawDocument] = []
        for e in entries:
            link = e.get("link")
            if not link or not self._ok(link):
                continue
            if self.keywords:
                hay = f"{e.get('title', '')} {e.get('summary', '')}".lower()
                if not any(k in hay for k in self.keywords):
                    continue
            self.entries_seen += 1
            if self.seen(link):
                continue
            try:
                doc = self.fetcher.fetch(link, discovered_via=f"feed:{objective}", title_hint=e.get("title"),
                                         published_hint=_entry_time(e))
            except RobotsDisallowed as ex:
                self.errors.append(f"robots: {link}: {ex}")
                continue
            except FetchError as ex:
                self.errors.append(f"fetch: {link}: {ex}")
                continue
            doc.meta["feed_title"] = feed.feed.get("title")
            doc.meta["entry_id"] = e.get("id")
            if e.get("summary"):
                doc.meta["feed_summary"] = e.get("summary")[:1000]
            out.append(doc)
            if len(out) >= budget.max_items:
                break
        return out

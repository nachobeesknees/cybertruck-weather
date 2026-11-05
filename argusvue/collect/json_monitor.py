"""JSON list monitor: a JSON endpoint lists items (meetings, releases) with document links; fetch the new
links (agenda/minutes PDFs) through the robots-aware fetcher."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from argusvue.collect.base import Budget, Collector, RawDocument
from argusvue.collect.fetcher import Fetcher, FetchError, RobotsDisallowed

log = logging.getLogger(__name__)


def _parse_date(v) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)).date()
    except ValueError:
        return None


class JsonListMonitor(Collector):
    kind = "json"

    def __init__(self, fetcher: Fetcher, *, seen: Callable[[str], bool], items: str = "", link_keys: list[str] | None = None,
                 date_key: str | None = None, title_keys: list[str] | None = None, keywords: list[str] | None = None,
                 max_age_days: int = 60, state: dict | None = None):
        self.fetcher = fetcher
        self.seen = seen
        self.items = items
        self.link_keys = link_keys or []
        self.date_key = date_key
        self.title_keys = title_keys or []
        self.keywords = [k.lower() for k in (keywords or [])]
        self.max_age_days = max_age_days
        self.state = state if state is not None else {}
        self.errors: list[str] = []
        self.items_seen = 0
        self.feed_unchanged = False

    def run(self, objective: str, schema: dict | None, geo_scope: str | None, budget: Budget) -> list[RawDocument]:
        content, _mime, _final = self.fetcher.get_bytes(objective)
        digest = hashlib.sha256(content).hexdigest()
        if self.state.get("json_hash") == digest:
            self.feed_unchanged = True
            return []
        self.state["json_hash"] = digest
        try:
            data = json.loads(content.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise FetchError(f"{objective} is not JSON: {e}") from e
        items = data
        for part in [p for p in self.items.split(".") if p]:
            items = items.get(part, []) if isinstance(items, dict) else []
        if not isinstance(items, list):
            raise FetchError(f"{objective}: items path {self.items!r} did not yield a list")
        today = datetime.now(UTC).date()
        out: list[RawDocument] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            d = _parse_date(it.get(self.date_key)) if self.date_key else None
            if d and (d < today - timedelta(days=self.max_age_days) or d > today + timedelta(days=1)):
                continue
            title = " ".join(str(it.get(k) or "") for k in self.title_keys).strip() or None
            if self.keywords and not any(k in (title or "").lower() for k in self.keywords):
                continue
            for key in self.link_keys:
                link = it.get(key)
                if not link or not isinstance(link, str) or not link.startswith("http"):
                    continue
                self.items_seen += 1
                if self.seen(link):
                    continue
                try:
                    doc = self.fetcher.fetch(link, discovered_via=f"json:{objective}", title_hint=title,
                                             published_hint=datetime(d.year, d.month, d.day, tzinfo=UTC) if d else None)
                except RobotsDisallowed as ex:
                    self.errors.append(f"robots: {link}: {ex}")
                    continue
                except FetchError as ex:
                    self.errors.append(f"fetch: {link}: {ex}")
                    continue
                doc.meta["json_item"] = {k: it.get(k) for k in (self.date_key, *self.title_keys) if k}
                out.append(doc)
                if len(out) >= budget.max_items:
                    return out
        return out

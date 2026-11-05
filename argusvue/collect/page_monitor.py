"""Index-page monitor for sites without feeds (agenda centers, newsroom lists): fetch the page, extract
links that match the allow patterns, fetch the ones we have not stored yet."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

from argusvue.collect.base import Budget, Collector, RawDocument
from argusvue.collect.fetcher import Fetcher, FetchError, RobotsDisallowed

log = logging.getLogger(__name__)
_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)


def extract_links(html: str, base_url: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in _HREF.finditer(html):
        href = m.group(1).strip()
        if href.lower().startswith(("mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).scheme not in ("http", "https"):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


class PageMonitor(Collector):
    kind = "page"

    def __init__(self, fetcher: Fetcher, *, seen: Callable[[str], bool], allow: list[str] | None = None,
                 deny: list[str] | None = None, state: dict | None = None, same_host_only: bool = False):
        self.fetcher = fetcher
        self.seen = seen
        self.allow = [re.compile(p) for p in (allow or [])]
        self.deny = [re.compile(p) for p in (deny or [])]
        self.state = state if state is not None else {}
        self.same_host_only = same_host_only
        self.errors: list[str] = []
        self.links_seen = 0
        self.feed_unchanged = False

    def _ok(self, url: str, base: str) -> bool:
        if self.same_host_only and urlparse(url).netloc != urlparse(base).netloc:
            return False
        if self.allow and not any(p.search(url) for p in self.allow):
            return False
        return not any(p.search(url) for p in self.deny)

    def run(self, objective: str, schema: dict | None, geo_scope: str | None, budget: Budget) -> list[RawDocument]:
        content, _mime, final = self.fetcher.get_bytes(objective)
        digest = hashlib.sha256(content).hexdigest()
        if self.state.get("page_hash") == digest:
            self.feed_unchanged = True
            return []
        self.state["page_hash"] = digest
        html = content.decode("utf-8", errors="replace")
        out: list[RawDocument] = []
        for link in extract_links(html, final):
            if not self._ok(link, final):
                continue
            self.links_seen += 1
            if self.seen(link):
                continue
            try:
                out.append(self.fetcher.fetch(link, discovered_via=f"page:{objective}"))
            except RobotsDisallowed as ex:
                self.errors.append(f"robots: {link}: {ex}")
            except FetchError as ex:
                self.errors.append(f"fetch: {link}: {ex}")
            if len(out) >= budget.max_items:
                break
        return out

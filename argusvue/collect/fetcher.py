"""Own fetcher: robots.txt (cached per host, our User-Agent), crawl delay, size cap, snapshot-ready
bytes, and text extraction (HTML via trafilatura, PDF via pdfplumber)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from argusvue.collect.base import RawDocument
from argusvue.settings import USER_AGENT

log = logging.getLogger(__name__)

_WIRE_PATTERNS = [
    (re.compile(r"(?:\bassociated press\b|\(ap\)|\bap news\b)", re.IGNORECASE), "wire:ap"),
    (re.compile(r"\breuters\b", re.IGNORECASE), "wire:reuters"),
    (re.compile(r"\bbloomberg\b", re.IGNORECASE), "wire:bloomberg"),
]
_PR_HOSTS = {"prnewswire.com", "businesswire.com", "globenewswire.com", "accesswire.com", "newswire.ca"}


class RobotsDisallowed(RuntimeError):
    pass


class FetchError(RuntimeError):
    pass


@dataclass
class RobotsInfo:
    allowed: bool
    crawl_delay: float | None


class RobotsCache:
    """Per-host robots.txt with a TTL. Unreachable robots.txt (network error) => allowed (standard
    practice), but a 5xx is treated as disallowed until it recovers."""

    def __init__(self, client: httpx.Client, ttl_s: float = 6 * 3600):
        self.client = client
        self.ttl_s = ttl_s
        self._cache: dict[str, tuple[float, robotparser.RobotFileParser | None]] = {}

    def _get(self, host: str, scheme: str) -> robotparser.RobotFileParser | None:
        now = time.monotonic()
        hit = self._cache.get(host)
        if hit and now - hit[0] < self.ttl_s:
            return hit[1]
        rp = robotparser.RobotFileParser()
        try:
            r = self.client.get(f"{scheme}://{host}/robots.txt", timeout=20.0)
            if r.status_code >= 500:
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except httpx.HTTPError as e:
            log.warning("robots.txt for %s unreachable (%s); treating as allowed", host, e)
            rp.allow_all = True
        self._cache[host] = (now, rp)
        return rp

    def check(self, url: str) -> RobotsInfo:
        u = urlparse(url)
        rp = self._get(u.netloc, u.scheme or "https")
        allowed = rp.can_fetch(USER_AGENT, url)
        delay = rp.crawl_delay(USER_AGENT)
        try:
            delay = float(delay) if delay is not None else None
        except (TypeError, ValueError):
            delay = None
        return RobotsInfo(allowed=allowed, crawl_delay=delay)


class Fetcher:
    def __init__(self, client: httpx.Client | None = None, *, default_delay_s: float = 2.0, max_bytes: int = 15 << 20,
                 robots: RobotsCache | None = None, strict_robots: bool = True):
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True)
        self.default_delay_s = default_delay_s
        self.max_bytes = max_bytes
        self.robots = robots or RobotsCache(self.client)
        self.strict_robots = strict_robots
        self._last: dict[str, float] = {}

    def _wait(self, host: str, delay: float) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = delay - (time.monotonic() - last)
            if gap > 0:
                time.sleep(gap)
        self._last[host] = time.monotonic()

    def get_bytes(self, url: str, *, check_robots: bool = True) -> tuple[bytes, str | None, str]:
        """(content, mime, final_url). Raises RobotsDisallowed / FetchError."""
        host = urlparse(url).netloc
        delay = self.default_delay_s
        if check_robots and self.strict_robots:
            info = self.robots.check(url)
            if not info.allowed:
                raise RobotsDisallowed(f"robots.txt disallows {url} for our User-Agent")
            if info.crawl_delay:
                delay = max(delay, info.crawl_delay)
        self._wait(host, delay)
        try:
            with self.client.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code} for {url}")
                buf = bytearray()
                for chunk in r.iter_bytes(1 << 16):
                    buf.extend(chunk)
                    if len(buf) > self.max_bytes:
                        raise FetchError(f"{url} exceeds {self.max_bytes} bytes")
                return bytes(buf), r.headers.get("content-type"), str(r.url)
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e

    def fetch(self, url: str, *, discovered_via: str | None = None, check_robots: bool = True,
              title_hint: str | None = None, published_hint: datetime | None = None) -> RawDocument:
        content, mime, final_url = self.get_bytes(url, check_robots=check_robots)
        text, title, published = extract_text(content, mime, final_url)
        return RawDocument(url=final_url, fetched_at=datetime.now(UTC), content=content, mime=mime, text=text,
                           title=title or title_hint, published_at=published or published_hint,
                           origin_key=origin_key_for(final_url, text), discovered_via=discovered_via,
                           meta={"requested_url": url})


def _is_pdf(content: bytes, mime: str | None, url: str) -> bool:
    return content[:5] == b"%PDF-" or (mime or "").split(";")[0].strip() == "application/pdf" or url.lower().endswith(".pdf")


def extract_text(content: bytes, mime: str | None, url: str) -> tuple[str, str | None, datetime | None]:
    """(text, title, published_at)."""
    if _is_pdf(content, mime, url):
        import io

        import pdfplumber

        pages = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
        return "\f".join(pages), None, None
    kind = (mime or "").split(";")[0].strip()
    html = content.decode("utf-8", errors="replace")
    if kind in ("text/html", "application/xhtml+xml", "") or "<html" in html[:2000].lower():
        import trafilatura

        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True, favor_recall=True) or ""
        title = published = None
        try:
            md = trafilatura.extract_metadata(html, default_url=url, extensive=False)
            if md is not None:
                title = md.title
            # Only explicit dates (meta tags, <time>, JSON-LD); never a year guessed from the body text.
            from htmldate import find_date

            d = find_date(html, extensive_search=False, original_date=True, outputformat="%Y-%m-%d", url=url)
            if d:
                published = datetime.fromisoformat(d).replace(tzinfo=UTC)
        except (ValueError, TypeError, AttributeError) as e:  # metadata is best-effort
            log.debug("metadata extraction failed for %s: %s", url, e)
        return text, title, published
    return html, None, None


def registrable_domain(host: str) -> str:
    parts = host.lower().split(":")[0].split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def origin_key_for(url: str, text: str) -> str:
    """Canonical origin so syndicated copies count once: wire service if detectable in the first
    lines, the issuing domain for press-release hosts, else the registrable domain."""
    host = urlparse(url).netloc
    dom = registrable_domain(host)
    head = text[:600]
    for pat, key in _WIRE_PATTERNS:
        if pat.search(head):
            return key
    if dom in _PR_HOSTS:
        return f"pr:{dom}"
    return dom

"""Fetchers: HTTP (streams to BlobStore while hashing) and fixture-backed (tests, offline)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

from argusvue.ingest.base import FetchSpec
from argusvue.settings import USER_AGENT
from argusvue.store.blobs import BlobRef, BlobStore


@dataclass
class FetchResult:
    blob: BlobRef
    mime: str | None
    status: int
    final_url: str


class HttpFetcher:
    """Polite streaming fetcher: descriptive User-Agent, a minimum delay between requests to the same
    host, and retries with backoff on transient failures (connection errors, 429, 5xx)."""

    RETRY_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def __init__(self, store: BlobStore, *, timeout: float = 600.0, client: httpx.Client | None = None,
                 delay_s: float = 1.0, retries: int = 3):
        self.store = store
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)
        self.delay_s = delay_s
        self.retries = retries
        self._last_by_host: dict[str, float] = {}

    def _wait(self, url: str) -> None:
        host = httpx.URL(url).host
        last = self._last_by_host.get(host)
        if last is not None:
            gap = self.delay_s - (time.monotonic() - last)
            if gap > 0:
                time.sleep(gap)
        self._last_by_host[host] = time.monotonic()

    def fetch(self, spec: FetchSpec) -> FetchResult:
        attempt = 0
        while True:
            attempt += 1
            self._wait(spec.url)
            try:
                with self.client.stream(spec.method, spec.url, content=spec.body, headers={**spec.headers}) as r:
                    if r.status_code in self.RETRY_STATUS and attempt <= self.retries:
                        raise httpx.HTTPStatusError(f"retryable status {r.status_code}", request=r.request, response=r)
                    r.raise_for_status()
                    blob = self.store.put_stream(r.iter_bytes(1 << 20), suffix=spec.suffix)
                    return FetchResult(blob=blob, mime=r.headers.get("content-type"), status=r.status_code, final_url=str(r.url))
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                retryable = isinstance(e, httpx.TransportError) or (
                    isinstance(e, httpx.HTTPStatusError) and e.response.status_code in self.RETRY_STATUS)
                if not retryable or attempt > self.retries:
                    raise
                backoff = 2.0 ** attempt
                log.warning("fetch %s failed (%s); retry %d/%d in %.0fs", spec.url, e, attempt, self.retries, backoff)
                time.sleep(backoff)


class FixtureFetcher:
    """Serves local files for given URLs. Unknown URLs raise, so a test never silently hits the network."""

    def __init__(self, store: BlobStore, files: dict[str, Path]):
        self.store = store
        self.files = dict(files)

    def fetch(self, spec: FetchSpec) -> FetchResult:
        if spec.url not in self.files:
            raise KeyError(f"no fixture for {spec.url}")
        p = Path(self.files[spec.url])
        blob = self.store.put_file(p, suffix=spec.suffix or p.suffix)
        return FetchResult(blob=blob, mime=None, status=200, final_url=spec.url)

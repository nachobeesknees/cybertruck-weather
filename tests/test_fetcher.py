from __future__ import annotations

import httpx
import pytest

from argusvue.collect.fetcher import (
    Fetcher,
    RobotsCache,
    RobotsDisallowed,
    extract_text,
    origin_key_for,
    registrable_domain,
)

ROBOTS = "User-agent: *\nDisallow: /private/\nCrawl-delay: 0\n"
HTML = ("<html><head><title>Council approves rezoning</title><meta property='article:published_time' "
        "content='2026-11-03T10:00:00'></head><body><article><h1>Council approves rezoning</h1><p>The Phoenix City "
        "Council approved a 320-unit apartment complex near Deer Valley Road on Wednesday, citing demand tied to the "
        "TSMC campus. The developer expects to deliver units by late 2027.</p></article></body></html>")


def _transport():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if p.startswith("/private/"):
            return httpx.Response(200, text="<html><body>secret</body></html>")
        if p == "/news/1":
            return httpx.Response(200, text=HTML, headers={"content-type": "text/html; charset=utf-8"})
        if p == "/big":
            return httpx.Response(200, content=b"x" * 2000, headers={"content-type": "application/octet-stream"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture
def fetcher():
    client = httpx.Client(transport=_transport(), headers={"User-Agent": "argusvue/0.1 test"})
    return Fetcher(client=client, default_delay_s=0, max_bytes=1000, robots=RobotsCache(client))


def test_robots_disallow_blocks_fetch(fetcher):
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("https://example.test/private/page")


def test_html_fetch_extracts_text_title_date_and_origin(fetcher):
    doc = fetcher.fetch("https://example.test/news/1", discovered_via="feed:x")
    assert "320-unit apartment complex" in doc.text
    assert doc.title == "Council approves rezoning"
    assert doc.published_at is not None and doc.published_at.year == 2026
    assert doc.origin_key == "example.test"
    assert doc.discovered_via == "feed:x" and doc.mime.startswith("text/html")


def test_size_cap(fetcher):
    from argusvue.collect.fetcher import FetchError

    with pytest.raises(FetchError, match="exceeds"):
        fetcher.fetch("https://example.test/big")


def test_origin_keys():
    assert origin_key_for("https://www.azcentral.com/story/x", "PHOENIX (AP) - Something happened") == "wire:ap"
    assert origin_key_for("https://www.prnewswire.com/news-releases/x", "TSMC announced") == "pr:prnewswire.com"
    assert origin_key_for("https://news.jhnewsandguide.com/a/b", "Local story") == "jhnewsandguide.com"
    assert registrable_domain("sub.example.co.uk") == "example.co.uk"


def test_pdf_text_extraction_path():
    # Minimal valid PDF with one text object (no external library needed to build it).
    content = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 100]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
               b"4 0 obj<</Length 44>>stream\nBT /F1 18 Tf 20 50 Td (Lodging tax 123) Tj ET\nendstream\nendobj\n"
               b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
               b"trailer<</Root 1 0 R>>\n%%EOF\n")
    text, title, _published = extract_text(content, "application/octet-stream", "https://example.test/report")
    assert "Lodging tax 123" in text and title is None

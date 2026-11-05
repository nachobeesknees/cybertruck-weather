"""Feed/page monitors with a mock HTTP transport, and the monitor runner against the test DB."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from argusvue.collect.base import Budget, MonitorConfig
from argusvue.collect.feed_monitor import FeedMonitor
from argusvue.collect.fetcher import Fetcher, RobotsCache
from argusvue.collect.page_monitor import PageMonitor, extract_links
from argusvue.store.blobs import BlobStore

FIX = Path(__file__).parent / "fixtures" / "collect"
BODY = ("The Phoenix City Council on Wednesday approved a rezoning request for a 320-unit apartment complex near Deer "
        "Valley Road, the third large multifamily approval in the north Phoenix corridor this year. Council members cited "
        "demand tied to the TSMC campus and its suppliers. The developer expects to break ground in the spring and deliver "
        "units by late 2027, according to filings with the planning department. Opponents raised traffic concerns at the "
        "hearing, and staff said a traffic study would be required before building permits are issued. ") * 3


def _html(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1><p>{body}</p></article></body></html>"


def _transport(pages: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        key = f"{request.url.host}{request.url.path}"
        if key in pages:
            body = pages[key]
            ct = "application/rss+xml" if body.lstrip().startswith("<?xml") else "text/html; charset=utf-8"
            return httpx.Response(200, text=body, headers={"content-type": ct})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _fetcher(pages):
    client = httpx.Client(transport=_transport(pages), headers={"User-Agent": "argusvue/0.1 test"})
    return Fetcher(client=client, default_delay_s=0, robots=RobotsCache(client))


PAGES = {
    "news.test/feed.xml": (FIX / "feed.xml").read_text(),
    "news.test/news/1": _html("Council approves rezoning", BODY),
    "news.test/news/2": _html("Old story", "Old body " * 50),
    "news.test/sports/3": _html("Suns win", "Basketball " * 50),
    "syndicate.test/wire/9": _html("Council approves rezoning", "PHOENIX (AP) - " + BODY + " Copyright 2026 The Associated Press."),
    "agendas.test/AgendaCenter": "<html><body><a href='/AgendaCenter/ViewFile/Agenda/_11032026-100'>Nov 3</a>"
                                 "<a href='/AgendaCenter/ViewFile/Minutes/_10202026-99'>Oct 20 minutes</a>"
                                 "<a href='mailto:x@y'>mail</a><a href='https://other.test/x'>other</a></body></html>",
    "agendas.test/AgendaCenter/ViewFile/Agenda/_11032026-100": _html("Agenda Nov 3", "Item 4: rezoning case Z-12-26 " * 30),
}


def test_feed_monitor_fetches_new_entries_and_skips_seen_and_filtered():
    f = _fetcher(PAGES)
    mon = FeedMonitor(f, seen=lambda u: u.endswith("/news/2"), deny=[r"/sports/"])
    docs = mon.run("https://news.test/feed.xml", None, None, Budget(max_items=10))
    assert [d.url for d in docs] == ["https://news.test/news/1"]
    d = docs[0]
    assert d.title == "Council approves rezoning" and d.published_at is not None and d.published_at.day == 3
    assert d.discovered_via == "feed:https://news.test/feed.xml" and d.meta["feed_title"] == "Test Local News"
    assert mon.entries_seen == 2  # sports filtered before counting
    # unchanged feed on the next run -> nothing
    mon2 = FeedMonitor(f, seen=lambda u: False, state=dict(mon.state))
    assert mon2.run("https://news.test/feed.xml", None, None, Budget()) == [] and mon2.feed_unchanged


def test_page_monitor_follows_matching_links_only():
    f = _fetcher(PAGES)
    mon = PageMonitor(f, seen=lambda u: False, allow=[r"/ViewFile/Agenda/"])
    docs = mon.run("https://agendas.test/AgendaCenter", None, None, Budget(max_items=5))
    assert [d.url for d in docs] == ["https://agendas.test/AgendaCenter/ViewFile/Agenda/_11032026-100"]
    assert "rezoning case" in docs[0].text
    links = extract_links(PAGES["agendas.test/AgendaCenter"], "https://agendas.test/AgendaCenter")
    assert "https://other.test/x" in links and not any(link.startswith("mailto") for link in links)


@pytest.mark.db
def test_runner_stores_dedups_and_updates_ledger(synthetic, tmp_path, monkeypatch):
    from argusvue.collect import runner as cr
    from argusvue.ingest import registry

    monitors = {
        "tst-news-feed": MonitorConfig(id="tst-news-feed", region="tst", kind="feed", target="https://news.test/feed.xml",
                                       cadence="daily", deny_patterns=[r"/sports/"], reliability=0.7),
        "tst-wire-page": MonitorConfig(id="tst-wire-page", region="tst", kind="page", target="https://syndicate.test/wire/9",
                                       cadence="daily", reliability=0.6),
    }
    monkeypatch.setattr(registry, "load_monitors", lambda *a, **k: monitors)
    store = BlobStore(tmp_path / "blobs")
    f = _fetcher(PAGES)
    st = cr.run_monitor(synthetic, monitors["tst-news-feed"], fetcher=f, store=store)
    assert st["new"] == 2 and st["exact_dup"] == 0 and st["errors"] == []
    with synthetic.cursor() as cur:
        cur.execute("SELECT l.last_result, l.checks, l.new_docs, l.next_due_at > now() AS due_later FROM attention_ledger l "
                    "JOIN sources s ON s.id = l.source_id WHERE s.slug = 'tst-news-feed'")
        led = cur.fetchone()
    assert led["last_result"] == "new:2" and led["checks"] == 1 and led["new_docs"] == 2 and led["due_later"]
    # second run: feed hash unchanged -> nothing fetched
    st2 = cr.run_monitor(synthetic, monitors["tst-news-feed"], fetcher=f, store=store)
    assert st2["new"] == 0 and st2["unchanged"] is True
    with synthetic.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM documents")
        assert cur.fetchone()["n"] == 2
    # a syndicated copy from another source is a near-duplicate inheriting the origin
    # (the 'url' kind fetches exactly the target)
    wire = MonitorConfig(id="tst-wire-page", region="tst", kind="url", target="https://syndicate.test/wire/9", cadence="daily")
    monitors["tst-wire-page"] = wire
    st3 = cr.run_monitor(synthetic, wire, fetcher=f, store=store)
    assert st3["near_dup"] == 1 and st3["new"] == 0
    with synthetic.cursor() as cur:
        cur.execute("SELECT origin_key, meta FROM documents WHERE url = 'https://syndicate.test/wire/9'")
        d = cur.fetchone()
        assert d["origin_key"] == "news.test" and d["meta"]["near_dup_of"] is not None
        cur.execute("SELECT count(*) AS n FROM job_runs WHERE job LIKE 'collect.%%' AND ok")
        assert cur.fetchone()["n"] == 3
    # due_monitors respects next_due_at
    assert cr.due_monitors(synthetic, "tst") == []
    assert [m.id for m in cr.due_monitors(synthetic, "tst", all_monitors=True)] == ["tst-news-feed", "tst-wire-page"]

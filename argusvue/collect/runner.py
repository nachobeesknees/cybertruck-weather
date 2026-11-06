"""Run monitors: pick the collector for a monitor, store documents (dedup, origin), update the attention
ledger and job_runs. Every monitor run is idempotent: seen URLs are skipped, identical content is an
exact duplicate, syndicated copies are near duplicates."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from argusvue.collect.base import Budget, MonitorConfig
from argusvue.collect.feed_monitor import FeedMonitor
from argusvue.collect.fetcher import Fetcher, RobotsDisallowed
from argusvue.collect.json_monitor import JsonListMonitor
from argusvue.collect.own_fetch import UrlCollector
from argusvue.collect.page_monitor import PageMonitor
from argusvue.collect.search import SearchCollector
from argusvue.ingest import registry
from argusvue.store.blobs import BlobStore
from argusvue.store.documents import store_raw_document
from argusvue.store.jobs import job_run

log = logging.getLogger(__name__)

CADENCE_DELTA = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(days=7)}


def url_seen(conn: psycopg.Connection, url: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM documents WHERE url = %s OR meta->>'requested_url' = %s LIMIT 1", (url, url))
        return cur.fetchone() is not None


def _ledger_state(conn, source_id: int, target: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM attention_ledger WHERE source_id = %s AND target = %s", (source_id, target))
        row = cur.fetchone()
    return dict(row["state"]) if row else {}


def _ledger_update(conn, source_id: int, target: str, result: str, cadence: str, new_docs: int, state: dict,
                   error: bool = False) -> None:
    now = datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO attention_ledger(source_id, target, last_checked_at, last_result, next_due_at, checks, new_docs,
                                            last_error_at, state)
               VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s)
               ON CONFLICT (source_id, target) DO UPDATE SET
                 last_checked_at = EXCLUDED.last_checked_at, last_result = EXCLUDED.last_result,
                 next_due_at = EXCLUDED.next_due_at, checks = attention_ledger.checks + 1,
                 new_docs = attention_ledger.new_docs + EXCLUDED.new_docs,
                 last_error_at = COALESCE(EXCLUDED.last_error_at, attention_ledger.last_error_at),
                 state = attention_ledger.state || EXCLUDED.state""",
            (source_id, target, now, result[:500], now + CADENCE_DELTA.get(cadence, timedelta(days=1)), new_docs,
             now if error else None, Jsonb(state)),
        )


def make_collector(conn, monitor: MonitorConfig, fetcher: Fetcher, state: dict, *, store=None, client=None):
    seen = lambda url: url_seen(conn, url)
    if monitor.kind == "feed":
        return FeedMonitor(fetcher, seen=seen, allow=monitor.allow_patterns, deny=monitor.deny_patterns, state=state,
                           keywords=monitor.keywords)
    if monitor.kind == "json":
        return JsonListMonitor(fetcher, seen=seen, items=monitor.json_items, link_keys=monitor.json_link_keys,
                               date_key=monitor.json_date_key, title_keys=monitor.json_title_keys, keywords=monitor.keywords,
                               max_age_days=monitor.max_age_days, state=state)
    if monitor.kind == "page":
        return PageMonitor(fetcher, seen=seen, allow=monitor.allow_patterns, deny=monitor.deny_patterns, state=state)
    if monitor.kind == "search":
        return SearchCollector(conn, fetcher, seen=seen, region_id=monitor.region, domains=monitor.domains, store=store,
                               client=client)
    if monitor.kind == "url":
        return UrlCollector(fetcher)
    raise ValueError(f"unknown monitor kind {monitor.kind!r}")


def run_monitor(conn: psycopg.Connection, monitor: MonitorConfig, *, fetcher: Fetcher | None = None,
                store: BlobStore | None = None, budget: Budget | None = None, client=None) -> dict[str, Any]:
    from argusvue.settings import get_settings

    if monitor.ai_bot_rule and get_settings().strict_robots:
        raise RobotsDisallowed(f"monitor {monitor.id}: host names AI crawlers in robots.txt and STRICT_ROBOTS is on")
    registry.sync(conn)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sources WHERE slug = %s", (monitor.id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"monitor {monitor.id} has no sources row (is it in config/monitors.yaml?)")
    source_id = row["id"]
    store = store or BlobStore()
    fetcher = fetcher or Fetcher()
    budget = budget or Budget(max_items=monitor.max_items)
    stats: dict[str, Any] = {"monitor": monitor.id, "kind": monitor.kind, "target": monitor.target, "new": 0, "exact_dup": 0,
                             "near_dup": 0, "errors": [], "unchanged": False, "cost_usd": 0.0}
    with job_run(conn, f"collect.{monitor.kind}", monitor.region) as run:
        state = _ledger_state(conn, source_id, monitor.target)
        collector = make_collector(conn, monitor, fetcher, state, store=store, client=client)
        try:
            docs = collector.run(monitor.target, None, monitor.geo_scope, budget)
        except RobotsDisallowed as e:
            _ledger_update(conn, source_id, monitor.target, f"robots_disallowed: {e}", monitor.cadence, 0, state, error=True)
            conn.commit()
            stats["errors"].append(f"robots_disallowed: {e}")
            run.stats.update(stats)
            return stats
        except Exception as e:
            from argusvue.llm import PendingAnswer

            if isinstance(e, PendingAnswer):
                _ledger_update(conn, source_id, monitor.target, f"pending model answer {e.request_id}", monitor.cadence, 0, state)
                conn.commit()
                stats["pending"] = e.request_id
                run.stats.update(stats)
                return stats
            _ledger_update(conn, source_id, monitor.target, f"error: {type(e).__name__}: {e}", monitor.cadence, 0, state,
                           error=True)
            conn.commit()
            raise
        for raw in docs:
            stored = store_raw_document(conn, store, raw, source_id, monitor.region)
            stats[stored.status] += 1
        stats["errors"].extend(getattr(collector, "errors", []))
        stats["unchanged"] = getattr(collector, "feed_unchanged", False)
        stats["cost_usd"] = getattr(collector, "cost_usd", 0.0)
        result = "unchanged" if stats["unchanged"] else (f"new:{stats['new']}" if stats["new"] else "nothing_new")
        if stats["errors"]:
            result += f" errors:{len(stats['errors'])}"
        _ledger_update(conn, source_id, monitor.target, result, monitor.cadence, stats["new"], state)
        conn.commit()
        run.stats.update(stats)
    return stats


def due_monitors(conn: psycopg.Connection, region_id: str, *, only: str | None = None, kind: str | None = None,
                 all_monitors: bool = False) -> list[MonitorConfig]:
    monitors = [m for m in registry.load_monitors().values() if m.region == region_id]
    if only:
        monitors = [m for m in monitors if m.id == only]
    if kind:
        monitors = [m for m in monitors if m.kind == kind]
    if all_monitors or only:
        return monitors
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.slug FROM attention_ledger l JOIN sources s ON s.id = l.source_id
               WHERE l.next_due_at > now()"""
        )
        not_due = {r["slug"] for r in cur.fetchall()}
    return [m for m in monitors if m.id not in not_due]

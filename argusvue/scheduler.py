"""In-process APScheduler. Every job is an idempotent CLI-equivalent function; runs land in job_runs.
Timezone: settings.timezone (America/Phoenix)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from argusvue.config import load_regions
from argusvue.ingest import registry
from argusvue.settings import get_settings

log = logging.getLogger(__name__)


def job_ingest(region_id: str, weekly: bool) -> None:
    from argusvue.ingest.runner import run_ingest
    from argusvue.store.db import connection

    for s in registry.load_sources().values():
        if not (s.active and s.module and s.applies_to(region_id)):
            continue
        monthly = (s.cadence or "").endswith("month") or (s.cadence or "").endswith("year")
        if monthly != weekly:
            continue
        try:
            with connection() as conn:
                run_ingest(conn, s.slug, region_id)
        except Exception:
            log.exception("ingest %s %s failed", s.slug, region_id)


def job_ingest_national() -> None:
    from argusvue.ingest.runner import run_ingest
    from argusvue.store.db import connection

    for s in registry.load_sources().values():
        if s.active and s.module and "*" in s.regions:
            try:
                with connection() as conn:
                    run_ingest(conn, s.slug, "*")
            except Exception:
                log.exception("ingest %s * failed", s.slug)


def job_collect_due(region_id: str) -> None:
    from argusvue.collect.runner import due_monitors, run_monitor
    from argusvue.store.db import connection

    with connection() as conn:
        monitors = due_monitors(conn, region_id)
    for m in monitors:
        try:
            with connection() as conn:
                run_monitor(conn, m)
        except Exception:
            log.exception("monitor %s failed", m.id)


def build(scheduler: BlockingScheduler | None = None) -> BlockingScheduler:
    tz = get_settings().timezone
    sched = scheduler or BlockingScheduler(timezone=tz)
    regions = list(load_regions().regions)
    for r in regions:
        sched.add_job(job_collect_due, CronTrigger(minute=7, timezone=tz), args=[r], id=f"collect_{r}",
                      max_instances=1, coalesce=True)
        sched.add_job(job_ingest, CronTrigger(hour=5, minute=30, timezone=tz), args=[r, False], id=f"ingest_daily_{r}",
                      max_instances=1, coalesce=True)
        sched.add_job(job_ingest, CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=tz), args=[r, True],
                      id=f"ingest_weekly_{r}", max_instances=1, coalesce=True)
    sched.add_job(job_ingest_national, CronTrigger(hour=5, minute=0, timezone=tz), id="ingest_national", max_instances=1)
    return sched


def start() -> None:
    sched = build()
    log.info("scheduler jobs: %s", [j.id for j in sched.get_jobs()])
    sched.start()

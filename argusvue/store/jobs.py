"""Every job run is recorded in job_runs, success or failure."""

from __future__ import annotations

import json
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg


@dataclass
class JobRun:
    id: int
    job: str
    region_id: str | None
    stats: dict = field(default_factory=dict)


@contextmanager
def job_run(conn: psycopg.Connection, job: str, region_id: str | None = None) -> Iterator[JobRun]:
    """Open a job_runs row, yield it so the job can fill `stats`, close it on exit. Commits on its own
    connection state: the row is written even when the job raises."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job_runs(job, region_id, started_at) VALUES (%s, %s, now()) RETURNING id",
            (job, region_id),
        )
        run = JobRun(id=cur.fetchone()["id"], job=job, region_id=region_id)
    conn.commit()
    try:
        yield run
    except BaseException as e:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE job_runs SET finished_at = now(), ok = false, stats = %s, error = %s WHERE id = %s",
                (json.dumps(run.stats, default=str), f"{type(e).__name__}: {e}\n{traceback.format_exc()}", run.id),
            )
        conn.commit()
        raise
    else:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE job_runs SET finished_at = now(), ok = true, stats = %s WHERE id = %s",
                (json.dumps(run.stats, default=str), run.id),
            )
        conn.commit()

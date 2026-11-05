"""Quant: anomalies, falsifier checks, lead-lag tests. Nightly job `quant.run`."""

from __future__ import annotations

import psycopg

from argusvue.quant.anomalies import compute_anomalies, latest_anomalies
from argusvue.quant.falsifiers import check_falsifiers
from argusvue.quant.leadlag import test_causal_edges
from argusvue.store.jobs import job_run

__all__ = ["check_falsifiers", "compute_anomalies", "latest_anomalies", "run_quant", "test_causal_edges"]


def run_quant(conn: psycopg.Connection, region_id: str) -> dict:
    with job_run(conn, "quant.run", region_id) as run:
        a = compute_anomalies(conn, region_id)
        f = check_falsifiers(conn, region_id)
        e = test_causal_edges(conn, region_id)
        run.stats.update({"anomalies": a, "falsifiers": f,
                          "edges": {k: v for k, v in e.items() if k != "results"}})
        merged = {"anomalies": a, "falsifiers": f, "edges": e}
    return merged

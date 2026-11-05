"""Falsifier checks: trigger or expire unresolved falsifiers of active theses."""

from __future__ import annotations

import operator as op
from datetime import date
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

OPS = {"<": op.lt, ">": op.gt, "<=": op.le, ">=": op.ge}


def check_falsifiers(conn: psycopg.Connection, region_id: str | None = None, *, today: date | None = None) -> dict:
    today = today or date.today()
    stats = {"checked": 0, "triggered": 0, "expired": 0, "no_data": 0}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT f.* FROM falsifiers f JOIN theses t ON t.id = f.thesis_id
               WHERE f.resolved_at IS NULL AND t.active AND (%(r)s::text IS NULL OR t.region_id = %(r)s)
               ORDER BY f.id""",
            {"r": region_id},
        )
        for f in cur.fetchall():
            cur.execute(
                """SELECT period, value FROM indicator_latest
                   WHERE indicator_id = %s AND geo_key = %s AND period <= %s ORDER BY period DESC LIMIT 1""",
                (f["indicator_id"], f["geo_key"], f["by_date"]),
            )
            pt = cur.fetchone()
            triggered = False
            if pt is None:
                stats["no_data"] += 1
            else:
                triggered = bool(OPS[f["operator"]](Decimal(pt["value"]), Decimal(f["threshold"])))
                cur.execute("INSERT INTO falsifier_checks(falsifier_id, period, value, triggered) VALUES (%s,%s,%s,%s)",
                            (f["id"], pt["period"], pt["value"], triggered))
                stats["checked"] += 1
            payload = {"falsifier_id": f["id"], "indicator_id": f["indicator_id"], "geo_key": f["geo_key"],
                       "value": float(pt["value"]) if pt else None, "threshold": float(f["threshold"]),
                       "operator": f["operator"], "action": f["action"]}
            if triggered:
                kind, outcome = "falsifier_triggered", "triggered"
            elif today > f["by_date"]:
                kind, outcome = "falsifier_expired", "expired"
            else:
                continue
            cur.execute("UPDATE falsifiers SET resolved_at = now(), outcome = %s WHERE id = %s", (outcome, f["id"]))
            cur.execute("INSERT INTO thesis_events(thesis_id, version, kind, payload) VALUES (%s,%s,%s,%s)",
                        (f["thesis_id"], f["version"], kind, Jsonb(payload)))
            stats[outcome] += 1
    conn.commit()
    return stats

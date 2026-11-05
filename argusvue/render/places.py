"""Place pages (SPEC §9): `out/places/<geo_key>.md` for the region CBSA, every submarket and every configured place."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import psycopg

from argusvue.render import RenderResult
from argusvue.render.common import (
    STATUS_ORDER,
    Ctx,
    attribute,
    defuse,
    finalize,
    indicator_series,
    indicator_table,
    make_ctx,
    thesis_bullet,
    thesis_rows,
)
from argusvue.render.tags import Tag

log = logging.getLogger(__name__)
EVENT_DAYS = 45
EVENT_LIMIT = 25


def place_keys(conn: psycopg.Connection, region_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT cbsa_code, config FROM regions WHERE id = %s", (region_id,))
        r = cur.fetchone()
        if r is None:
            raise KeyError(f"unknown region {region_id!r}")
        cur.execute("SELECT geo_key FROM submarkets WHERE region_id = %s ORDER BY geo_key", (region_id,))
        subs = [x["geo_key"] for x in cur.fetchall()]
    keys = [f"cbsa:{r['cbsa_code']}"] + subs + [f"place:{c}" for c in (r["config"] or {}).get("places", [])]
    return list(dict.fromkeys(keys))


def _related(ctx: Ctx, geo_key: str) -> list[str]:
    """The unit itself, its ancestors and its descendants (for a submarket: its member ZCTAs)."""
    with ctx.conn.cursor() as cur:
        cur.execute("SELECT ancestor_key AS k FROM geo_ancestry WHERE geo_key = %s UNION SELECT geo_key FROM geo_ancestry WHERE ancestor_key = %s",
                    (geo_key, geo_key))
        return [geo_key] + [r["k"] for r in cur.fetchall()]


def _theses(ctx: Ctx, geo_key: str) -> list[str]:
    rows = thesis_rows(ctx, _related(ctx, geo_key))
    rows.sort(key=lambda r: (STATUS_ORDER.index(r["status"]), r["slug"]))
    ctx.stats["theses"] = len(rows)
    out = ["## Current theses", ""]
    if not rows:
        return out + ["> No theses touch this unit.", ""]
    return out + [thesis_bullet(ctx, r) for r in rows] + [""]


def _snapshot(ctx: Ctx, geo_key: str) -> list[str]:
    rows = sorted(indicator_series(ctx, [geo_key]), key=lambda r: r["name"])
    ctx.stats["indicators"] = len(rows)
    out = ["## Indicator snapshot", ""]
    if not rows:
        return out + ["> No indicator points at exactly this geo key.", ""]
    return out + indicator_table(rows, with_geo=False) + [""]


def _events(ctx: Ctx, geo_key: str) -> list[str]:
    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.predicate, c.value_num, c.value_text, c.unit, e.canonical_name, d.title, d.url
               FROM claims c JOIN claim_geo g ON g.claim_id = c.id AND g.geo_key = %(g)s
                    JOIN entities e ON e.id = c.subject_entity JOIN documents d ON d.id = c.document_id
               WHERE c.extractor_version LIKE 'extract@%%' AND c.observed_at > %(lo)s
                 AND (%(cut)s::timestamptz IS NULL OR c.observed_at <= %(cut)s)
               ORDER BY c.observed_at DESC, c.id DESC LIMIT %(n)s""",
            {"g": geo_key, "lo": ctx.win_end - timedelta(days=EVENT_DAYS), "cut": ctx.cutoff, "n": EVENT_LIMIT},
        )
        rows = cur.fetchall()
    ctx.stats["events"] = len(rows)
    out = ["## Recent events", ""]
    if not rows:
        return out + [f"> No document-derived claims in the last {EVENT_DAYS} days.", ""]
    for r in rows:
        if r["value_num"] is not None:
            val = f"{float(r['value_num']):g}{(' ' + r['unit']) if r['unit'] else ''}"
        else:
            val = defuse(r["value_text"]) or "n/a"
        text = f"{defuse(r['canonical_name'])}: {r['predicate']} = {val} ({defuse(r['title']) or r['url']})"
        out.append("- " + attribute(text, [Tag("c", str(r["id"]))]))
    return out + [""]


def render_places(conn: psycopg.Connection, region_id: str, as_of: date | None = None, out_dir: Path | None = None) -> list[RenderResult]:
    ctx = make_ctx(conn, region_id, as_of, out_dir)
    results = []
    for key in place_keys(conn, region_id):
        with conn.cursor() as cur:
            cur.execute("SELECT name, level FROM geo_units WHERE geo_key = %s", (key,))
            g = cur.fetchone()
        if g is None:
            log.warning("place %s is not in geo_units; skipped", key)
            continue
        ctx.stats = {}
        lines = [f"# {g['name'] or key} (`{key}`)", "",
                 f"> Level {g['level']}, region `{region_id}`, rendered {ctx.day.isoformat()}"
                 + (" as of that day's end." if as_of else " from current data."), ""]
        for section in (_theses, _snapshot, _events):
            lines += section(ctx, key)
        path = ctx.out_dir / "places" / f"{key.replace(':', '_')}.md"
        _, dropped = finalize(ctx, "\n".join(lines), path)
        results.append(RenderResult(path=path, dropped=dropped, stats={**ctx.stats, "dropped": len(dropped)}))
    conn.rollback()
    return results

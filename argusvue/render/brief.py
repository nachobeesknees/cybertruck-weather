"""Daily region brief (SPEC §9). Bi-temporal: with `as_of`, only data observed by the end of that local day is used."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import psycopg

from argusvue.render import RenderResult
from argusvue.render.common import (
    STATUS_ORDER,
    Ctx,
    attribute,
    claim_phrase,
    finalize,
    fmt_num,
    indicator_series,
    indicator_table,
    make_ctx,
    one_line,
    point_tag,
    support_claim_tags,
    thesis_bullet,
    thesis_rows,
)
from argusvue.render.tags import Tag


def region_geo_keys(conn: psycopg.Connection, region_id: str) -> list[str]:
    """CBSA key, plus county keys for regions with a commuter shed (jh), whose county series carry the data."""
    with conn.cursor() as cur:
        cur.execute("SELECT cbsa_code, county_fips, config FROM regions WHERE id = %s", (region_id,))
        r = cur.fetchone()
    keys = [f"cbsa:{r['cbsa_code']}"]
    if (r["config"] or {}).get("commuter_shed_counties"):
        keys += [f"county:{c}" for c in r["county_fips"]]
    return keys


def _what_changed(ctx: Ctx) -> list[str]:
    out = ["## What changed since yesterday", ""]
    n = 0
    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT v.thesis_id, v.version, v.status, v.change_summary, v.created_at, t.slug, t.approved_version,
                      (SELECT p.status FROM thesis_versions p WHERE p.thesis_id = v.thesis_id AND p.version < v.version
                       ORDER BY p.version DESC LIMIT 1) AS prev_status
               FROM thesis_versions v JOIN theses t ON t.id = v.thesis_id
               WHERE t.active AND t.region_id = %(r)s AND v.created_at > %(lo)s AND v.created_at <= %(hi)s
               ORDER BY v.created_at, v.thesis_id""",
            {"r": ctx.region_id, "lo": ctx.win_start, "hi": ctx.win_end},
        )
        versions = cur.fetchall()
    out += ["### Thesis versions", ""]
    if not versions:
        out += ["> No new thesis versions in the last 24 hours.", ""]
    for v in versions:
        n += 1
        move = f"{v['prev_status']} to {v['status']}" if v["prev_status"] and v["prev_status"] != v["status"] else v["status"]
        pend = ", pending human approval" if v["version"] > v["approved_version"] else ""
        summary = one_line(v["change_summary"])
        label = f"**{v['slug']}** v{v['version']} ({move}{pend})"
        tags = support_claim_tags(ctx, v["thesis_id"], v["version"])
        if tags:
            out.append(f"- {label}: " + attribute(summary, tags, first_only=True))
        else:
            out.append(f"> {label}: {summary} (no cited claims)")
    out.append("")

    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.kind, c.claim_a, c.claim_b, a.predicate, a.geo_key, e.canonical_name,
                      a.value_num AS an, a.value_text AS at, a.unit AS au, b.value_num AS bn, b.value_text AS bt, b.unit AS bu
               FROM conflicts c JOIN claims a ON a.id = c.claim_a JOIN claims b ON b.id = c.claim_b
                    JOIN entities e ON e.id = a.subject_entity
               WHERE c.region_id = %(r)s AND c.opened_at > %(lo)s AND c.opened_at <= %(hi)s ORDER BY c.id""",
            {"r": ctx.region_id, "lo": ctx.win_start, "hi": ctx.win_end},
        )
        conflicts = cur.fetchall()
    out += ["### New conflicts", ""]
    if not conflicts:
        out += ["> No new conflicts in the last 24 hours.", ""]
    for c in conflicts:
        n += 1
        out.append(f"- Conflict #{c['id']} ({c['kind']}): sources disagree on {c['predicate']} for {c['canonical_name']} at `{c['geo_key']}`, "
                   f"{claim_phrase(c['an'], c['at'], c['au'])} versus {claim_phrase(c['bn'], c['bt'], c['bu'])} "
                   f"[c:{c['claim_a']}] [c:{c['claim_b']}].")
    out.append("")

    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (f.id) f.id, t.slug, f.indicator_id, f.geo_key, f.operator, f.threshold, f.action,
                      c.period, c.value, i.frequency, i.unit
               FROM falsifier_checks c JOIN falsifiers f ON f.id = c.falsifier_id JOIN theses t ON t.id = f.thesis_id
                    JOIN indicators i ON i.id = f.indicator_id
               WHERE c.triggered AND c.period IS NOT NULL AND t.active AND t.region_id = %(r)s
                 AND c.checked_at > %(lo)s AND c.checked_at <= %(hi)s
               ORDER BY f.id, c.checked_at DESC, c.id DESC""",
            {"r": ctx.region_id, "lo": ctx.win_start, "hi": ctx.win_end},
        )
        fals = cur.fetchall()
    out += ["### Falsifiers triggered", ""]
    if not fals:
        out += ["> No falsifiers triggered in the last 24 hours.", ""]
    for f in fals:
        n += 1
        tag = point_tag(f["indicator_id"], f["period"], f["frequency"])
        out.append(f"- Falsifier on **{f['slug']}** triggered: {f['indicator_id']} at `{f['geo_key']}` was {fmt_num(float(f['value']))} "
                   f"in {tag.period}, past the limit ({f['operator']} {fmt_num(float(f['threshold']))}); action: {f['action']} {tag}.")
    out.append("")
    ctx.stats["changed"] = n
    return out


def _theses(ctx: Ctx) -> list[str]:
    rows = thesis_rows(ctx)
    out = ["## Theses", ""]
    if not rows:
        return out + ["> No active theses. Run `argusvue theses seed`.", ""]
    for status in STATUS_ORDER:
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        out += [f"### {status.capitalize()}", ""]
        out += [thesis_bullet(ctx, r) for r in group]
        out.append("")
    ctx.stats["theses"] = len(rows)
    return out


def _indicators(ctx: Ctx) -> list[str]:
    geos = region_geo_keys(ctx.conn, ctx.region_id)
    rows = indicator_series(ctx, geos)
    order = {g: n for n, g in enumerate(geos)}
    rows.sort(key=lambda r: (order[r["geo_key"]], r["name"]))
    ctx.stats["indicator_rows"] = len(rows)
    out = ["## Indicators", ""]
    if not rows:
        return out + ["> No indicator points observed yet for this region.", ""]
    return out + indicator_table(rows, with_geo=True) + [""]


def _conflicts_open(ctx: Ctx) -> list[str]:
    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.kind, c.claim_a, c.claim_b, a.predicate, a.geo_key, e.canonical_name,
                      a.value_num AS an, a.value_text AS at, a.unit AS au, b.value_num AS bn, b.value_text AS bt, b.unit AS bu
               FROM conflicts c JOIN claims a ON a.id = c.claim_a JOIN claims b ON b.id = c.claim_b
                    JOIN entities e ON e.id = a.subject_entity
               WHERE c.region_id = %(r)s AND (%(cut)s::timestamptz IS NULL OR c.opened_at <= %(cut)s)
                 AND (c.resolved_at IS NULL OR (%(cut)s::timestamptz IS NOT NULL AND c.resolved_at > %(cut)s))
               ORDER BY c.opened_at, c.id""",
            {"r": ctx.region_id, "cut": ctx.cutoff},
        )
        rows = cur.fetchall()
    ctx.stats["open_conflicts"] = len(rows)
    out = ["## Open conflicts", ""]
    if not rows:
        return out + ["> No open conflicts.", ""]
    for c in rows:
        out.append(f"- #{c['id']} ({c['kind']}) {c['predicate']} for {c['canonical_name']} at `{c['geo_key']}`: "
                   f"{claim_phrase(c['an'], c['at'], c['au'])} [c:{c['claim_a']}] versus {claim_phrase(c['bn'], c['bt'], c['bu'])} [c:{c['claim_b']}].")
    return out + [""]


def _questions(ctx: Ctx) -> list[str]:
    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT id, geo_key, question, raised_by_run FROM open_questions
               WHERE region_id = %(r)s AND (%(cut)s::timestamptz IS NULL OR created_at <= %(cut)s)
                 AND (status = 'open' OR (%(cut)s::timestamptz IS NOT NULL AND resolved_at > %(cut)s))
               ORDER BY created_at, id""",
            {"r": ctx.region_id, "cut": ctx.cutoff},
        )
        rows = cur.fetchall()
    ctx.stats["open_questions"] = len(rows)
    out = ["## Open questions", ""]
    if not rows:
        return out + ["> No open questions.", ""]
    for q in rows:
        text = one_line(q["question"])
        where = f"`{q['geo_key']}`: " if q["geo_key"] else ""
        if q["raised_by_run"]:
            out.append("- " + attribute(where + text, [Tag("run", str(q["raised_by_run"]))]))
        else:
            out.append(f"> Question #{q['id']}: {where}{text} (no raising run recorded)")
    return out + [""]


def _cost(ctx: Ctx) -> list[str]:
    with ctx.conn.cursor() as cur:
        cur.execute("SELECT role, model, calls, cost_usd FROM spend_daily WHERE day = %s ORDER BY cost_usd DESC, role", (ctx.day,))
        rows = cur.fetchall()
        cur.execute(
            """SELECT (SELECT count(*) FROM agent_runs WHERE (created_at AT TIME ZONE %(tz)s)::date = %(d)s AND error ILIKE '%%spend cap%%')
                    + (SELECT count(*) FROM job_runs WHERE (started_at AT TIME ZONE %(tz)s)::date = %(d)s AND error LIKE '%%SpendCapExceeded%%') AS n""",
            {"tz": str(ctx.tz), "d": ctx.day},
        )
        capped = cur.fetchone()["n"] > 0
    total = sum(float(r["cost_usd"]) for r in rows)
    ctx.stats["cost_usd"] = round(total, 4)
    out = ["## Cost today", "", f"> Spend for {ctx.day.isoformat()} ({ctx.tz.key} day), all regions.", ""]
    if rows:
        out += ["| role | model | calls | usd |", "|---|---|---|---|"]
        out += [f"| {r['role']} | {r['model']} | {r['calls']} | ${float(r['cost_usd']):,.4f} |" for r in rows]
        out.append(f"| **total** | | {sum(r['calls'] for r in rows)} | **${total:,.4f}** |")
    else:
        out.append("> No paid calls recorded for this day.")
    if capped:
        out += ["", "> Daily spend cap reached; jobs stopped."]
    return out + [""]


def render_brief(conn: psycopg.Connection, region_id: str, as_of: date | None = None, out_dir: Path | None = None) -> RenderResult:
    ctx = make_ctx(conn, region_id, as_of, out_dir)
    lines = [f"# {ctx.region_name} brief, {ctx.day.isoformat()}", "",
             f"> Region `{region_id}`. " + (f"Rendered as of end of {as_of.isoformat()}: only data observed by then is used."
                                            if as_of else "Rendered from current data."), ""]
    for section in (_what_changed, _theses, _indicators, _conflicts_open, _questions, _cost):
        lines += section(ctx)
    path = ctx.out_dir / "briefs" / f"{ctx.day.isoformat()}-{region_id}.md"
    _, dropped = finalize(ctx, "\n".join(lines), path)
    stats = {**ctx.stats, "dropped": len(dropped)}
    conn.rollback()      # read-only; end the snapshot transaction
    return RenderResult(path=path, dropped=dropped, stats=stats)

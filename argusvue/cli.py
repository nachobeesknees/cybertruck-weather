"""argusvue CLI. Commands are added phase by phase; see docs/SPEC.md §11."""

from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.table import Table

from argusvue.settings import get_settings

app = typer.Typer(no_args_is_help=True, help="Argusvue: living synthesis layer for regional real estate.")
db_app = typer.Typer(no_args_is_help=True, help="Database")
geo_app = typer.Typer(no_args_is_help=True, help="Geography")
indicators_app = typer.Typer(no_args_is_help=True, help="Indicator series")
sources_app = typer.Typer(no_args_is_help=True, help="Sources")
collect_app = typer.Typer(no_args_is_help=True, help="Web monitors and collectors")
scheduler_app = typer.Typer(no_args_is_help=True, help="Scheduler")
reconcile_app = typer.Typer(no_args_is_help=True, help="Claim reconciliation")
conflicts_app = typer.Typer(no_args_is_help=True, help="Contested claims")
extract_app = typer.Typer(no_args_is_help=True, help="Claim extraction from documents")
eval_app = typer.Typer(no_args_is_help=True, help="Evaluation harness")
app.add_typer(db_app, name="db")
app.add_typer(geo_app, name="geo")
app.add_typer(indicators_app, name="indicators")
app.add_typer(sources_app, name="sources")
app.add_typer(collect_app, name="collect")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(reconcile_app, name="reconcile")
app.add_typer(conflicts_app, name="conflicts")
app.add_typer(extract_app, name="extract")
app.add_typer(eval_app, name="eval")
console = Console()


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging")) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending SQL migrations."""
    from argusvue.store.db import connection
    from argusvue.store.migrate import migrate

    with connection() as conn:
        applied = migrate(conn)
    if applied:
        for m in applied:
            console.print(f"applied {m.version:04d}_{m.name}")
    else:
        console.print("up to date")


@geo_app.command("load")
def geo_load(region: str = typer.Option(..., "--region", help="Region id from config/regions.yaml")) -> None:
    """Load the gazetteer (TIGER/Line + OMB delineation) and submarkets for a region. Idempotent."""
    from argusvue.geo.load import load_region
    from argusvue.store.db import connection

    with connection() as conn:
        stats = load_region(conn, region)
    t = Table(title=f"geo load {region}")
    t.add_column("stat")
    t.add_column("value", justify="right")
    for k, v in stats.items():
        t.add_row(k, str(v))
    console.print(t)


@geo_app.command("resolve")
def geo_resolve(key: str = typer.Argument(..., help="geo_key like zcta:85085 or h3:872a1072bffffff")) -> None:
    """Show a key's record, its ancestors at every level, submarket memberships and H3 cells."""
    from argusvue.geo.keys import GeoKeyError
    from argusvue.geo.resolve import resolve
    from argusvue.store.db import connection

    try:
        with connection() as conn:
            r = resolve(conn, key)
    except GeoKeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e
    console.print(f"[bold]{r.key}[/bold]  level={r.level}  name={r.name!r}  region={r.region_id}  parent={r.parent_key}")
    if r.lat is not None:
        console.print(f"rep_point: {r.lat:.5f}, {r.lon:.5f}")
    t = Table(title="ancestors")
    t.add_column("level")
    t.add_column("geo_key")
    t.add_column("name")
    for a in r.ancestors:
        t.add_row(a["level"], a["geo_key"], a["name"] or "")
    console.print(t)
    console.print(f"submarkets: {', '.join(r.submarkets) or '-'}")
    console.print(f"h3: {', '.join(r.h3) or '-'}")


@app.command("ingest")
def ingest(
    source: str = typer.Argument(..., help="Source slug from config/sources.yaml, or 'all'"),
    region: str = typer.Option(..., "--region", help="Region id, or '*' for national context sources"),
    backfill: bool = typer.Option(False, "--backfill", help="Full history instead of the incremental window"),
    force: bool = typer.Option(False, "--force", help="Re-parse documents already ingested"),
) -> None:
    """Fetch a structured source, snapshot it, and upsert indicator points as cited claims. Idempotent."""
    from argusvue.ingest import registry
    from argusvue.ingest.runner import run_ingest
    from argusvue.store.db import connection

    slugs = [source]
    if source == "all":
        slugs = [s.slug for s in registry.load_sources().values()
                 if s.active and s.module and (s.applies_to(region) if region != "*" else "*" in s.regions)]
    t = Table(title=f"ingest {region}")
    for col in ("source", "docs", "new docs", "skipped", "parsed", "dropped", "inserted", "revised", "unchanged", "ok"):
        t.add_column(col, justify="right" if col != "source" else "left")
    failures = 0
    for slug in slugs:
        try:
            with connection() as conn:
                st = run_ingest(conn, slug, region, backfill=backfill, force=force)
            t.add_row(slug, str(st["fetched"]), str(st["new_documents"]), str(st["skipped"]), str(st["parsed"]),
                      str(st["dropped_out_of_region"]), str(st["inserted"]), str(st["revised"]), str(st["unchanged"]), "yes")
        except Exception as e:
            failures += 1
            logging.getLogger("argusvue.cli").exception("ingest %s failed", slug)
            t.add_row(slug, "", "", "", "", "", "", "", "", f"[red]FAILED: {type(e).__name__}[/red]")
    console.print(t)
    if failures:
        raise typer.Exit(code=1)


@indicators_app.command("list")
def indicators_list(region: str = typer.Option(None, "--region", help="Filter by region id")) -> None:
    """Indicators with coverage: series count, earliest/latest period, points."""
    from argusvue.store.db import connection, fetch_all

    sql = """
        SELECT i.id, i.unit, i.frequency, s.slug AS source, i.region_scope, i.low_confidence,
               count(DISTINCT p.geo_key) AS series, min(p.period) AS first, max(p.period) AS last, count(p.*) AS points
        FROM indicators i LEFT JOIN sources s ON s.id = i.source_id
        LEFT JOIN indicator_latest p ON p.indicator_id = i.id
          AND (%(region)s IS NULL OR p.geo_key IN (SELECT geo_key FROM geo_units WHERE region_id = %(region)s)
               OR p.geo_key = 'country:US')
        WHERE %(region)s IS NULL OR i.region_scope @> ARRAY[%(region)s] OR i.region_scope @> ARRAY['*']
        GROUP BY i.id, i.unit, i.frequency, s.slug, i.region_scope, i.low_confidence ORDER BY i.id"""
    with connection() as conn:
        rows = fetch_all(conn, sql, {"region": region})
    t = Table(title=f"indicators{' ' + region if region else ''}")
    for col in ("id", "unit", "freq", "source", "scope", "series", "first", "last", "points"):
        t.add_column(col, justify="right" if col in ("series", "points") else "left")
    for r in rows:
        t.add_row(r["id"] + (" (low conf)" if r["low_confidence"] else ""), r["unit"], r["frequency"], r["source"] or "",
                  ",".join(r["region_scope"]), str(r["series"]), str(r["first"] or ""), str(r["last"] or ""), str(r["points"]))
    console.print(t)


@indicators_app.command("show")
def indicators_show(
    indicator_id: str = typer.Argument(...),
    geo: str = typer.Option(None, "--geo", help="geo_key; omit to list available series"),
    last: int = typer.Option(24, "--last", help="Number of most recent periods"),
    revisions: bool = typer.Option(False, "--revisions", help="Show every observed revision, not just current belief"),
) -> None:
    """Show a series: current belief per period with the claim id each value traces to."""
    from argusvue.store.db import connection, fetch_all

    with connection() as conn:
        if geo is None:
            rows = fetch_all(conn, """SELECT p.geo_key, g.name, min(period) AS first, max(period) AS last, count(*) AS n
                                      FROM indicator_latest p LEFT JOIN geo_units g USING (geo_key)
                                      WHERE indicator_id = %s GROUP BY 1, 2 ORDER BY 1""", (indicator_id,))
            t = Table(title=f"{indicator_id}: series")
            for col in ("geo_key", "name", "first", "last", "points"):
                t.add_column(col)
            for r in rows:
                t.add_row(r["geo_key"], r["name"] or "", str(r["first"]), str(r["last"]), str(r["n"]))
            console.print(t)
            return
        table = "indicator_points" if revisions else "indicator_latest"
        rows = fetch_all(conn, f"""SELECT p.period, p.value, p.claim_id, p.observed_at, c.document_id, c.status
                                   FROM {table} p JOIN claims c ON c.id = p.claim_id
                                   WHERE p.indicator_id = %s AND p.geo_key = %s
                                   ORDER BY p.period DESC, p.observed_at DESC LIMIT %s""", (indicator_id, geo, last))
    t = Table(title=f"{indicator_id} @ {geo}")
    for col in ("period", "value", "claim", "status", "doc", "observed_at"):
        t.add_column(col, justify="right" if col == "value" else "left")
    for r in reversed(rows):
        t.add_row(str(r["period"]), f"{r['value']:,}", f"c:{r['claim_id']}", r["status"], f"d:{r['document_id']}",
                  r["observed_at"].strftime("%Y-%m-%d %H:%M"))
    console.print(t)


@sources_app.command("list")
def sources_list() -> None:
    """Configured sources and their document counts."""
    from argusvue.ingest import registry
    from argusvue.store.db import connection, fetch_all

    with connection() as conn:
        registry.sync(conn)
        rows = fetch_all(conn, """SELECT s.slug, s.kind, s.region_id, s.reliability, s.origin_key, s.active,
                                         count(d.*) AS docs, max(d.fetched_at) AS last_fetch
                                  FROM sources s LEFT JOIN documents d ON d.source_id = s.id
                                  GROUP BY s.id ORDER BY s.slug""")
    t = Table(title="sources")
    for col in ("slug", "kind", "region", "reliability", "origin", "active", "docs", "last fetch"):
        t.add_column(col)
    for r in rows:
        t.add_row(r["slug"], r["kind"], r["region_id"] or "*", str(r["reliability"]), r["origin_key"], str(r["active"]),
                  str(r["docs"]), str(r["last_fetch"] or ""))
    console.print(t)


@collect_app.command("run")
def collect_run(
    region: str = typer.Option(..., "--region"),
    source: str = typer.Option(None, "--source", help="Run one monitor id"),
    kind: str = typer.Option(None, "--kind", help="feed|page|search|url"),
    all_monitors: bool = typer.Option(False, "--all", help="Ignore the attention ledger's next_due_at"),
    max_items: int = typer.Option(None, "--max-items"),
) -> None:
    """Run the region's due monitors (or one), store new documents, update the attention ledger."""
    from argusvue.collect.base import Budget
    from argusvue.collect.runner import due_monitors, run_monitor
    from argusvue.store.db import connection

    with connection() as conn:
        monitors = due_monitors(conn, region, only=source, kind=kind, all_monitors=all_monitors)
    t = Table(title=f"collect {region}")
    for col in ("monitor", "kind", "new", "exact dup", "near dup", "unchanged", "errors", "cost", "ok"):
        t.add_column(col)
    failures = 0
    for m in monitors:
        try:
            with connection() as conn:
                st = run_monitor(conn, m, budget=Budget(max_items=max_items or m.max_items))
            t.add_row(m.id, m.kind, str(st["new"]), str(st["exact_dup"]), str(st["near_dup"]), str(st["unchanged"]),
                      str(len(st["errors"])), f"{st['cost_usd']:.3f}", "yes")
        except Exception as e:
            failures += 1
            logging.getLogger("argusvue.cli").exception("monitor %s failed", m.id)
            t.add_row(m.id, m.kind, "", "", "", "", "", "", f"[red]FAILED: {type(e).__name__}[/red]")
    console.print(t)
    if not monitors:
        console.print("nothing due (use --all to force)")
    if failures:
        raise typer.Exit(code=1)


@collect_app.command("status")
def collect_status(region: str = typer.Option(..., "--region")) -> None:
    """Attention ledger: last check, result and next due time per monitor."""
    from argusvue.store.db import connection, fetch_all

    with connection() as conn:
        rows = fetch_all(conn, """SELECT s.slug, s.kind, l.last_checked_at, l.last_result, l.next_due_at, l.checks, l.new_docs
                                  FROM attention_ledger l JOIN sources s ON s.id = l.source_id
                                  WHERE s.region_id = %s ORDER BY s.slug""", (region,))
    t = Table(title=f"attention ledger {region}")
    for col in ("monitor", "kind", "last checked", "result", "next due", "checks", "new docs"):
        t.add_column(col)
    for r in rows:
        t.add_row(r["slug"], r["kind"], r["last_checked_at"].strftime("%m-%d %H:%M") if r["last_checked_at"] else "",
                  r["last_result"] or "", r["next_due_at"].strftime("%m-%d %H:%M") if r["next_due_at"] else "",
                  str(r["checks"]), str(r["new_docs"]))
    console.print(t)


@extract_app.command("run")
def extract_run(region: str = typer.Option(None, "--region"), limit: int = typer.Option(50, "--limit"),
                document: int = typer.Option(None, "--document", help="Extract one document id"),
                force: bool = typer.Option(False, "--force")) -> None:
    """Extract cited claims from collected web documents (skips duplicates and structured downloads)."""
    from argusvue.extract.extractor import extract_document, run_extract
    from argusvue.store.db import connection

    with connection() as conn:
        if document is not None:
            st = extract_document(conn, document, force=force)
            console.print(st.__dict__)
        else:
            console.print(run_extract(conn, region_id=region, limit=limit, force=force))


@eval_app.command("precision")
def eval_precision(sample_n: int = typer.Option(0, "--sample", help="Write a new audit CSV with N claims"),
                   region: str = typer.Option(None, "--region"), since_days: int = typer.Option(7, "--since-days")) -> None:
    """Sample claims for human labelling (--sample N) and report precision from labelled audit files."""
    from argusvue.eval import precision
    from argusvue.store.db import connection

    if sample_n:
        with connection() as conn:
            path = precision.sample(conn, n=sample_n, since_days=since_days, region_id=region)
        console.print(f"wrote {path}")
    console.print(precision.compute())


@reconcile_app.command("run")
def reconcile_run(region: str = typer.Option(None, "--region"),
                  include_structured: bool = typer.Option(False, "--include-structured")) -> None:
    """Reconcile unreconciled claims: corroborate / merge / contest / supersede (SPEC §6)."""
    from argusvue.reconcile.engine import run_reconcile, set_half_lives
    from argusvue.store.db import connection

    with connection() as conn:
        filled = set_half_lives(conn)
        st = run_reconcile(conn, region_id=region, include_structured=include_structured)
    console.print({k: v for k, v in st.items()} | {"half_lives_filled": filled})


@conflicts_app.command("list")
def conflicts_list(region: str = typer.Option(None, "--region"), show_all: bool = typer.Option(False, "--all")) -> None:
    """Open conflicts between contested claims."""
    from argusvue.store.db import connection, fetch_all

    with connection() as conn:
        rows = fetch_all(conn, """SELECT k.id, k.kind, k.opened_at, k.resolved_at, k.resolution, k.attempts, k.geo_key,
                                         a.predicate, a.value_num AS a_num, a.value_text AS a_text, a.document_id AS a_doc,
                                         b.value_num AS b_num, b.value_text AS b_text, b.document_id AS b_doc, k.claim_a, k.claim_b
                                  FROM conflicts k JOIN claims a ON a.id = k.claim_a JOIN claims b ON b.id = k.claim_b
                                  WHERE (%s::text IS NULL OR k.region_id = %s) AND (%s OR k.resolved_at IS NULL)
                                  ORDER BY k.opened_at DESC LIMIT 200""", (region, region, show_all))
    t = Table(title="conflicts")
    for col in ("id", "kind", "predicate", "geo", "claim a", "claim b", "opened", "status", "attempts"):
        t.add_column(col)
    for r in rows:
        va = r["a_text"] if r["a_text"] is not None else r["a_num"]
        vb = r["b_text"] if r["b_text"] is not None else r["b_num"]
        t.add_row(str(r["id"]), r["kind"], r["predicate"], r["geo_key"] or "", f"c:{r['claim_a']}={va} (d:{r['a_doc']})",
                  f"c:{r['claim_b']}={vb} (d:{r['b_doc']})", r["opened_at"].strftime("%Y-%m-%d"),
                  r["resolution"] or "open", str(r["attempts"]))
    console.print(t)


@conflicts_app.command("resolve")
def conflicts_resolve(conflict_id: int = typer.Argument(...),
                      keep: str = typer.Option(..., "--keep", help="a | b | both | neither"),
                      note: str = typer.Option("", "--note")) -> None:
    """Resolve a conflict by hand: the kept claim(s) return to asserted, the other is retracted."""
    from argusvue.store.db import connection, execute, fetch_one

    if keep not in ("a", "b", "both", "neither"):
        raise typer.BadParameter("--keep must be a, b, both or neither")
    with connection() as conn:
        k = fetch_one(conn, "SELECT * FROM conflicts WHERE id = %s", (conflict_id,))
        if k is None:
            raise typer.BadParameter(f"no conflict {conflict_id}")
        a, b = k["claim_a"], k["claim_b"]
        keep_ids = {"a": [a], "b": [b], "both": [a, b], "neither": []}[keep]
        drop_ids = [c for c in (a, b) if c not in keep_ids]
        if keep_ids:
            execute(conn, "UPDATE claims SET status = 'asserted' WHERE id = ANY(%s) AND status = 'contested'", (keep_ids,))
        if drop_ids:
            execute(conn, "UPDATE claims SET status = 'retracted' WHERE id = ANY(%s)", (drop_ids,))
        resolution = {"a": "keep_a", "b": "keep_b", "both": "both_valid", "neither": "retract_both"}[keep]
        execute(conn, "UPDATE conflicts SET resolved_at = now(), resolution = %s, notes = %s WHERE id = %s",
                (resolution, note or None, conflict_id))
    console.print(f"conflict {conflict_id}: {resolution}")


@scheduler_app.command("start")
def scheduler_start() -> None:
    """Run the in-process scheduler (blocking)."""
    from argusvue.scheduler import start

    start()


@app.command("spend")
def spend(days: int = typer.Option(7, "--days")) -> None:
    """Spend per local day and role against the daily cap."""
    from argusvue.store.db import connection, fetch_all

    s = get_settings()
    with connection() as conn:
        rows = fetch_all(conn, """SELECT day, role, model, calls, input_tokens, output_tokens, search_uses, cost_usd
                                  FROM spend_daily WHERE day >= current_date - %s ORDER BY day DESC, cost_usd DESC""", (days,))
    t = Table(title=f"spend (cap ${s.daily_spend_cap_usd:.2f}/day, {s.timezone})")
    for col in ("day", "role", "model", "calls", "in", "out", "searches", "usd"):
        t.add_column(col, justify="right" if col not in ("day", "role", "model") else "left")
    for r in rows:
        t.add_row(str(r["day"]), r["role"], r["model"], str(r["calls"]), str(r["input_tokens"]), str(r["output_tokens"]),
                  str(r["search_uses"]), f"{float(r['cost_usd']):.4f}")
    console.print(t)
    if not rows:
        console.print("no paid calls yet")


if __name__ == "__main__":
    app()

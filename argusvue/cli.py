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
app.add_typer(db_app, name="db")
app.add_typer(geo_app, name="geo")
app.add_typer(indicators_app, name="indicators")
app.add_typer(sources_app, name="sources")
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


@app.command("spend")
def spend() -> None:
    """Today's LLM/collector spend against the cap. (Ledger arrives with phase 2.)"""
    s = get_settings()
    console.print(f"cap: ${s.daily_spend_cap_usd:.2f}/day; spend ledger not yet implemented (phase 2)")


if __name__ == "__main__":
    app()

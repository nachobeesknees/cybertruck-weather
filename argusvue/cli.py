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
app.add_typer(db_app, name="db")
app.add_typer(geo_app, name="geo")
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


@app.command("spend")
def spend() -> None:
    """Today's LLM/collector spend against the cap. (Ledger arrives with phase 2.)"""
    s = get_settings()
    console.print(f"cap: ${s.daily_spend_cap_usd:.2f}/day; spend ledger not yet implemented (phase 2)")


if __name__ == "__main__":
    app()

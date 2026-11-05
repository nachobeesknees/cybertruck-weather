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
theses_app = typer.Typer(no_args_is_help=True, help="Theses: seed, list, show, review, approve, reject")
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
app.add_typer(theses_app, name="theses")
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


@theses_app.command("seed")
def theses_seed(region: str = typer.Option(None, "--region")) -> None:
    """Create the seed theses from config/theses_seed.yaml (idempotent)."""
    from argusvue.agents.theses import seed
    from argusvue.store.db import connection

    with connection() as conn:
        console.print(seed(conn, region_id=region))


@theses_app.command("list")
def theses_list(region: str = typer.Option(None, "--region")) -> None:
    """Current (rendered) version of every thesis with status, confidence and the human gate state."""
    from argusvue.agents.theses import current_versions
    from argusvue.store.db import connection

    with connection() as conn:
        rows = current_versions(conn, region)
    t = Table(title="theses")
    for col in ("id", "slug", "geo", "domain", "status", "conf", "v", "human", "pending change"):
        t.add_column(col)
    for r in rows:
        t.add_row(str(r["id"]), r["slug"], r["geo_key"], r["domain"], r["status"], f"{float(r['confidence']):.2f}",
                  f"{r['rendered_version']}/{r['current_version']}", r["human_status"], "yes" if r["pending_human"] else "")
    console.print(t)


@theses_app.command("show")
def theses_show(thesis_id: int = typer.Argument(...), version: int = typer.Option(None, "--version")) -> None:
    """Full detail of a thesis version: statement, evidence, causal edges, falsifiers, decisions."""
    from argusvue.agents.theses import version_detail
    from argusvue.store.db import connection

    with connection() as conn:
        d = version_detail(conn, thesis_id, version)
    t, v = d["thesis"], d["version"]
    console.print(f"[bold]{t['slug']}[/bold] (#{t['id']}) {t['region_id']} {t['geo_key']} {t['domain']}  "
                  f"current v{t['current_version']} approved v{t['approved_version']} human={t['human_status']}")
    console.print(f"v{v['version']} [{v['status']}] confidence {float(v['confidence']):.2f}  ({v['critic_verdict']}, trigger={v['trigger']})")
    console.print(v["statement"])
    console.print(f"change: {v['change_summary']}")
    console.print(f"reasoning: {v['confidence_reasoning']}")
    for imp in v["implications"]:
        console.print(f"  implication: {imp.get('text')} cites={imp.get('cites')} horizon={imp.get('horizon_months')}m" + (" [hypothesis]" if imp.get("hypothesis") else ""))
    console.print(f"support: {[c['claim_id'] for c in d['claims'] if c['role'] == 'support']}  counter: {[c['claim_id'] for c in d['claims'] if c['role'] == 'counter']}")
    for e in d["edges"]:
        console.print(f"  edge: {e['cause']} -> {e['effect']} ({e['strength']}, lag {e['lag_months']}m, {e['status']}) {e['mechanism']}" + (f" test={e['test_result']}" if e["test_result"] else ""))
    for f in d["falsifiers"]:
        console.print(f"  falsifier #{f['id']}: {f['indicator_id']} @ {f['geo_key']} {f['operator']} {f['threshold']} by {f['by_date']} -> {f['action']}"
                      f"  last={f['last_value']} triggered={f['last_triggered']} {('[' + f['outcome'] + ']') if f['outcome'] else ''}")
    if v["known_disputes"]:
        console.print(f"known disputes: {v['known_disputes']}")
    for dec in d["decisions"]:
        console.print(f"  human: v{dec['version']} {dec['decision']} {dec['decided_at']:%Y-%m-%d} {dec['note'] or ''}")


@theses_app.command("review")
def theses_review(thesis_id: int = typer.Argument(...), force: bool = typer.Option(False, "--force", help="Skip triage"),
                  dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Run Triage -> Analyst -> Validator -> Critic -> Gate for one thesis (fully logged in thesis_events)."""
    from argusvue.agents.review import review_thesis
    from argusvue.store.db import connection
    from argusvue.store.jobs import job_run

    with connection() as conn, job_run(conn, "theses.review", None) as run:
        o = review_thesis(conn, thesis_id, trigger="manual", force=force, dry_run=dry_run)
        run.stats.update(o.summary())
    console.print(o.summary())
    if o.proposal:
        console.print(f"[bold]{o.proposal.action}[/bold] -> {o.proposal.status} ({o.proposal.confidence:.2f}): {o.proposal.statement}")
        console.print(f"change: {o.proposal.change_summary}")
    if o.critic:
        console.print(f"critic: {o.critic.verdict} - {o.critic.summary}")


@theses_app.command("review-due")
def theses_review_due(region: str = typer.Option(None, "--region"), limit: int = typer.Option(20, "--limit"),
                      dry: bool = typer.Option(False, "--list", help="Only list what is due")) -> None:
    """Review every thesis whose triggers fired (falsifier, new claims, anomalies, weekly schedule)."""
    from argusvue.agents.review import due_theses, review_due
    from argusvue.store.db import connection

    with connection() as conn:
        if dry:
            for t in due_theses(conn, region):
                console.print(f"#{t['id']} {t['slug']}: {', '.join(t['reasons'])}")
            return
        for r in review_due(conn, region, limit=limit):
            console.print(r)


@theses_app.command("events")
def theses_events(thesis_id: int = typer.Argument(...), last: int = typer.Option(30, "--last")) -> None:
    """The review log for a thesis: every triage, analyst, validator, critic and gate step with run ids."""
    from argusvue.store.db import connection, fetch_all

    with connection() as conn:
        rows = fetch_all(conn, "SELECT * FROM thesis_events WHERE thesis_id = %s ORDER BY id DESC LIMIT %s", (thesis_id, last))
    for r in reversed(rows):
        console.print(f"{r['created_at']:%Y-%m-%d %H:%M} v{r['version'] or '-'} {r['kind']:<12} run={r['run_id'] or '-'} {str(r['payload'])[:300]}")


@app.command("replay")
def replay_cmd(as_of: str = typer.Option(..., "--as-of", help="YYYY-MM-DD"), thesis: int = typer.Option(..., "--thesis")) -> None:
    """Rebuild the packet with evidence observed on or before AS_OF and run the loop in dry-run mode."""
    from datetime import date

    from argusvue.eval.replay import replay
    from argusvue.store.db import connection

    with connection() as conn:
        console.print(replay(conn, thesis, date.fromisoformat(as_of)))


@theses_app.command("approve")
def theses_approve(thesis_id: int = typer.Argument(...), note: str = typer.Option("", "--note")) -> None:
    """Human gate: approve the current version (required before a status change renders as current)."""
    from argusvue.agents.theses import decide
    from argusvue.store.db import connection

    with connection() as conn:
        console.print(decide(conn, thesis_id, "approved", note or None))


@theses_app.command("reject")
def theses_reject(thesis_id: int = typer.Argument(...), note: str = typer.Option("", "--note")) -> None:
    """Human gate: reject the current version; the last approved version stays current."""
    from argusvue.agents.theses import decide
    from argusvue.store.db import connection

    with connection() as conn:
        console.print(decide(conn, thesis_id, "rejected", note or None))


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


# ---- quant ----------------------------------------------------------------------------------------
quant_app = typer.Typer(no_args_is_help=True, help="Quant: anomalies, falsifier checks, lead-lag tests")
app.add_typer(quant_app, name="quant")


@quant_app.command("run")
def quant_run(region: str = typer.Option(..., "--region")) -> None:
    """Recompute anomalies, check falsifiers, test causal edges for a region."""
    from argusvue.quant import run_quant
    from argusvue.store.db import connection

    with connection() as conn:
        res = run_quant(conn, region)
    console.print({"anomalies": res["anomalies"], "falsifiers": res["falsifiers"],
                   "edges": {k: v for k, v in res["edges"].items() if k != "results"}})
    for r in res["edges"]["results"]:
        console.print(r)


@quant_app.command("anomalies")
def quant_anomalies(region: str = typer.Option(..., "--region"), min_z: float = typer.Option(2.0, "--min-z"),
                    limit: int = typer.Option(30, "--limit")) -> None:
    """Biggest |z| moves in the latest quant batch."""
    from argusvue.quant import latest_anomalies
    from argusvue.store.db import connection

    with connection() as conn:
        rows = latest_anomalies(conn, region, min_abs_z=min_z, limit=limit)
    t = Table(title=f"anomalies {region} (|z| >= {min_z})")
    for col in ("indicator", "geo", "period", "value", "unit", "z", "yoy%", "mom%"):
        t.add_column(col)
    f = lambda x: "" if x is None else f"{float(x):.1f}"
    for r in rows:
        t.add_row(r["name"], r["geo_key"], str(r["period"]), f"{float(r['value']):,.2f}", r["unit"],
                  f"{float(r['zscore']):.2f}", f(r["yoy_pct"]), f(r["mom_pct"]))
    console.print(t)


@quant_app.command("falsifiers")
def quant_falsifiers(region: str = typer.Option(None, "--region")) -> None:
    """Falsifiers with their latest check."""
    from argusvue.store.db import connection, fetch_all

    with connection() as conn:
        rows = fetch_all(conn, """SELECT f.id, t.slug, f.indicator_id, f.geo_key, f.operator, f.threshold, f.by_date, f.outcome,
                                         c.checked_at, c.period, c.value, c.triggered
                                  FROM falsifiers f JOIN theses t ON t.id = f.thesis_id
                                  LEFT JOIN LATERAL (SELECT * FROM falsifier_checks WHERE falsifier_id = f.id
                                                     ORDER BY checked_at DESC, id DESC LIMIT 1) c ON true
                                  WHERE %(r)s::text IS NULL OR t.region_id = %(r)s ORDER BY f.id""", {"r": region})
    t = Table(title="falsifiers")
    for col in ("id", "thesis", "indicator", "geo", "rule", "by", "last value", "period", "triggered", "outcome"):
        t.add_column(col)
    for r in rows:
        t.add_row(str(r["id"]), r["slug"], r["indicator_id"], r["geo_key"], f"{r['operator']} {float(r['threshold']):,.2f}",
                  str(r["by_date"]), "" if r["value"] is None else f"{float(r['value']):,.2f}",
                  str(r["period"] or ""), "" if r["triggered"] is None else str(r["triggered"]), r["outcome"] or "open")
    console.print(t)


@eval_app.command("recall")
def eval_recall(region: str = typer.Option(None, "--region")) -> None:
    """Share of curated events (eval/events.yaml) captured, and median capture latency."""
    from rich.table import Table

    from argusvue.eval import recall
    from argusvue.store.db import connection

    with connection() as conn:
        res = recall.compute(conn, region)
    t = Table("event", "region", "date", "captured", "latency_d", "matches", "first match")
    for e in res["events"]:
        t.add_row(e["id"], e["region"], e["date"], "yes" if e["captured"] else "no", str(e["latency_days"]), str(e["matches"]),
                  str(e["first_match"]))
    console.print(t)
    console.print({"regions": res["regions"], "overall": res["overall"]})


@eval_app.command("dedup")
def eval_dedup(region: str = typer.Option(None, "--region")) -> None:
    """Raw -> unique -> extracted documents -> claims; re-extraction rate."""
    from rich.table import Table

    from argusvue.eval import dedup
    from argusvue.store.db import connection

    with connection() as conn:
        res = dedup.compute(conn, region)
    rows = {**res["regions"], "ALL": res["overall"]}
    cols = list(res["overall"])
    t = Table("region", *cols)
    for rid, r in rows.items():
        t.add_row(rid, *[str(r[c]) for c in cols])
    console.print(t)


@eval_app.command("staleness")
def eval_staleness(region: str = typer.Option(None, "--region")) -> None:
    """Median age and % past half-life of claims behind approved theses."""
    from argusvue.eval import staleness
    from argusvue.store.db import connection

    with connection() as conn:
        console.print(staleness.compute(conn, region))


@eval_app.command("conflicts")
def eval_conflicts(region: str = typer.Option(None, "--region")) -> None:
    """Conflicts opened / resolved / time-to-resolve."""
    from argusvue.eval import conflicts
    from argusvue.store.db import connection

    with connection() as conn:
        console.print(conflicts.compute(conn, region))


@eval_app.command("calibration")
def eval_calibration(region: str = typer.Option(None, "--region")) -> None:
    """Falsifier outcomes by action and Brier score (needs >= 10 resolved)."""
    from argusvue.eval import calibration
    from argusvue.store.db import connection

    with connection() as conn:
        console.print(calibration.compute(conn, region))


@eval_app.command("cost")
def eval_cost(days: int = typer.Option(7, "--days"), region: str = typer.Option(None, "--region")) -> None:
    """Spend per region-day by role/model, job runs by adapter, and today's cap usage."""
    from rich.table import Table

    from argusvue.eval import cost
    from argusvue.store.db import connection

    with connection() as conn:
        res = cost.compute(conn, region, days=days)
    t = Table("day", "region", "role", "model", "calls", "in_tok", "out_tok", "usd")
    for r in res["llm"]:
        t.add_row(r["day"], r["region"], r["role"], r["model"], str(r["calls"]), str(r["input_tokens"]), str(r["output_tokens"]),
                  f"{r['usd']:.4f}")
    tot = res["totals"]
    t.add_row("TOTAL", "", "", "", str(tot["calls"]), str(tot["input_tokens"]), str(tot["output_tokens"]), f"{tot['usd']:.4f}")
    console.print(t)
    j = Table("day", "region", "adapter", "runs", "failures")
    for r in res["jobs"]:
        j.add_row(r["day"], r["region"], r["adapter"], str(r["runs"]), str(r["failures"]))
    j.add_row("TOTAL", "", "", str(tot["job_runs"]), str(tot["job_failures"]))
    console.print(j)
    console.print({"today": res["today"]})


# ---- render: daily brief and place pages (SPEC §9) -----------------------------------------------------------
places_app = typer.Typer(no_args_is_help=True, help="Place pages")
app.add_typer(places_app, name="places")


def _parse_date(value: str | None):
    from datetime import date

    return date.fromisoformat(value) if value else None


@app.command("brief")
def brief(region: str = typer.Option(..., "--region"),
          date: str = typer.Option(None, "--date", help="Render as of the end of this day (YYYY-MM-DD); default: now")) -> None:
    """Write out/briefs/YYYY-MM-DD-<region>.md from claim/indicator ids only."""
    from argusvue.render.brief import render_brief
    from argusvue.store.db import connection

    with connection() as conn:
        res = render_brief(conn, region, _parse_date(date))
    console.print(f"wrote {res.path}  stats={res.stats}")
    for s in res.dropped:
        console.print(f"[yellow]dropped[/yellow] {s}")


@places_app.command("render")
def places_render(region: str = typer.Option(..., "--region"),
                  date: str = typer.Option(None, "--date", help="Render as of the end of this day (YYYY-MM-DD)")) -> None:
    """Write out/places/<geo_key>.md for the region CBSA, its submarkets and configured places."""
    from argusvue.render.places import render_places
    from argusvue.store.db import connection

    with connection() as conn:
        results = render_places(conn, region, _parse_date(date))
    dropped = sum(len(r.dropped) for r in results)
    console.print(f"wrote {len(results)} pages to {results[0].path.parent if results else '-'}; dropped sentences: {dropped}")
    for r in results:
        for s in r.dropped:
            console.print(f"[yellow]dropped[/yellow] {r.path.name}: {s}")

"""Narrative intelligence report (editor role) on top of the ledger-faithful brief. Every sentence must cite
claim or indicator ids from the packet's allowlist; the tag enforcer deletes anything else."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from argusvue import llm
from argusvue.render import RenderResult
from argusvue.render.common import (
    Ctx,
    finalize,
    indicator_series,
    indicator_table,
    make_ctx,
    thesis_bullet,
    thesis_rows,
)
from argusvue.settings import PACKAGE_ROOT

PROMPT = PACKAGE_ROOT / "agents" / "prompts" / "editor.md"
MAX_CLAIMS = 60
RECENT_DAYS = 45


class EditorBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headline: str = Field(min_length=5, max_length=160)
    bottom_line: list[str] = Field(min_length=1)
    what_moved: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    watchlist: list[str] = Field(default_factory=list)
    notes_for_operator: str = ""


@dataclass
class EditorPacket:
    theses: list[dict]
    indicators: list[dict]
    claims: list[dict]
    conflicts: list[dict]
    falsifiers: list[dict]
    allow_claims: list[int]
    allow_indicators: list[str]

    def to_prompt(self, ctx: Ctx) -> str:
        return json.dumps({
            "region": ctx.region_name, "region_id": ctx.region_id, "date": ctx.day.isoformat(),
            "theses": self.theses, "indicators": self.indicators, "recent_claims": self.claims,
            "open_conflicts": self.conflicts, "falsifiers": self.falsifiers,
            "allowlist": {"claims": self.allow_claims, "indicator_tags": self.allow_indicators},
        }, default=str, indent=1)


def _key_indicators(ctx: Ctx) -> list[dict]:
    with ctx.conn.cursor() as cur:
        cur.execute("SELECT cbsa_code, county_fips FROM regions WHERE id = %s", (ctx.region_id,))
        r = cur.fetchone()
    geos = [f"cbsa:{r['cbsa_code']}"] + [f"county:{c}" for c in r["county_fips"]] + ["country:US"]
    rows = indicator_series(ctx, geos)
    rows.sort(key=lambda x: (-(abs(x["z"]) if x["z"] is not None else 0), x["indicator_id"]))
    return rows


def build_editor_packet(ctx: Ctx) -> EditorPacket:
    cut = ctx.cutoff
    theses = []
    for t in thesis_rows(ctx):
        with ctx.conn.cursor() as cur:
            cur.execute("""SELECT c.id, c.predicate, coalesce(c.value_text, c.value_num::text) AS value, c.unit, left(c.excerpt, 240) AS excerpt
                           FROM thesis_claims tc JOIN claims c ON c.id = tc.claim_id
                           WHERE tc.thesis_id = %s AND tc.version = %s AND tc.role = 'support' ORDER BY c.id""", (t["id"], t["version"]))
            support = cur.fetchall()
            cur.execute("""SELECT f.indicator_id, f.geo_key, f.operator, f.threshold, f.by_date, f.action, f.outcome,
                                  (SELECT json_build_object('period', c.period, 'value', c.value, 'triggered', c.triggered)
                                     FROM falsifier_checks c WHERE c.falsifier_id = f.id AND (%s::timestamptz IS NULL OR c.checked_at <= %s)
                                     ORDER BY c.checked_at DESC LIMIT 1) AS last_check
                           FROM falsifiers f WHERE f.thesis_id = %s AND f.version = %s""", (cut, cut, t["id"], t["version"]))
            fals = cur.fetchall()
        theses.append({"id": t["id"], "slug": t["slug"], "geo_key": t["geo_key"], "domain": t["domain"], "status": t["status"],
                       "confidence": float(t["confidence"]), "statement": t["statement"], "change_summary": t["change_summary"],
                       "pending_human": t["pending_human"], "support": [dict(s) for s in support], "falsifiers": [dict(f) for f in fals]})
    indicators = [{"indicator_id": r["indicator_id"], "name": r["name"], "geo_key": r["geo_key"], "period": r["period"].isoformat(),
                   "value": r["value"], "unit": r["unit"], "mom_pct": r["mom"], "yoy_pct": r["yoy"], "zscore": r["z"], "tag": str(r["tag"])}
                  for r in _key_indicators(ctx)]
    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.predicate, e.canonical_name AS subject, coalesce(c.value_text, c.value_num::text) AS value, c.unit, c.geo_key,
                      c.valid_from, c.confidence, c.status, left(c.excerpt, 300) AS excerpt, d.title, d.url, coalesce(d.origin_key, s.origin_key) AS origin,
                      c.observed_at::date AS observed
               FROM claims c JOIN entities e ON e.id = c.subject_entity JOIN documents d ON d.id = c.document_id LEFT JOIN sources s ON s.id = d.source_id
               WHERE c.extractor_version LIKE 'extract@%%' AND c.status NOT IN ('retracted','superseded')
                 AND (%(cut)s::timestamptz IS NULL OR c.observed_at <= %(cut)s)
                 AND c.observed_at > coalesce(%(cut)s, now()) - make_interval(days => %(days)s)
                 AND EXISTS (SELECT 1 FROM claim_geo g JOIN geo_units u ON u.geo_key = g.geo_key WHERE g.claim_id = c.id AND u.region_id = %(r)s)
               ORDER BY c.confidence DESC, c.observed_at DESC LIMIT %(n)s""",
            {"cut": cut, "days": RECENT_DAYS, "r": ctx.region_id, "n": MAX_CLAIMS},
        )
        claims = [dict(r) | {"confidence": float(r["confidence"])} for r in cur.fetchall()]
        cur.execute(
            """SELECT k.id, k.kind, k.claim_a, k.claim_b, a.predicate, coalesce(a.value_text, a.value_num::text) AS value_a,
                      coalesce(b.value_text, b.value_num::text) AS value_b, k.opened_at::date AS opened
               FROM conflicts k JOIN claims a ON a.id = k.claim_a JOIN claims b ON b.id = k.claim_b
               WHERE k.region_id = %s AND k.resolved_at IS NULL AND (%s::timestamptz IS NULL OR k.opened_at <= %s) ORDER BY k.opened_at DESC LIMIT 20""",
            (ctx.region_id, cut, cut),
        )
        conflicts = [dict(r) for r in cur.fetchall()]
    falsifiers = [f | {"thesis": t["slug"]} for t in theses for f in t["falsifiers"]]
    allow_claims = sorted({c["id"] for c in claims} | {s["id"] for t in theses for s in t["support"]} | {k["claim_a"] for k in conflicts} | {k["claim_b"] for k in conflicts})
    allow_ind = sorted({i["tag"] for i in indicators})
    return EditorPacket(theses, indicators, claims, conflicts, falsifiers, allow_claims, allow_ind)


def render_intel(conn: psycopg.Connection, region_id: str, as_of: date | None = None, out_dir: Path | None = None,
                 client=None, store=None) -> RenderResult:
    ctx = make_ctx(conn, region_id, as_of, out_dir)
    packet = build_editor_packet(ctx)
    user = (f"Write today's report. Return JSON matching this schema:\n{json.dumps(EditorBrief.model_json_schema())}\n\nPACKET:\n"
            f"{packet.to_prompt(ctx)}")
    res = llm.complete(conn, "editor", system=PROMPT.read_text(encoding="utf-8"), user=user, schema=EditorBrief, region_id=region_id,
                       client=client, store=store, max_tokens=6000)
    b = res.parsed
    provenance = (f"> Every sentence below cites the ledger: `[c:id]` is a claim with a verbatim source excerpt, "
                  f"`[i:indicator:period]` an indicator point. Rendered as of end of {ctx.day.isoformat()}; "
                  f"prepared by the editor role (run {res.run_id}).")
    lines = [f"# {ctx.region_name}: intelligence report, {ctx.day.isoformat()}", "", f"> **{b.headline}**", "", provenance, "",
             "## Bottom line", ""]
    lines += [" ".join(b.bottom_line), ""]
    for title, items in (("What moved", b.what_moved), ("Signals", b.signals), ("Risks and contradictions", b.risks), ("Watchlist", b.watchlist)):
        lines += [f"## {title}", ""]
        lines += [f"- {it}" for it in items] if items else ["> Nothing to report."]
        lines.append("")
    if b.notes_for_operator.strip():
        lines += ["## Note for the operator", "", f"> {b.notes_for_operator.strip()}", ""]
    lines += ["## Theses", ""]
    rows = thesis_rows(ctx)
    lines += [thesis_bullet(ctx, r) for r in rows] if rows else ["> No theses."]
    lines += ["", "## Key indicators", ""]
    rows_i = _key_indicators(ctx)
    lines += indicator_table(rows_i[:40], with_geo=True) if rows_i else ["> No indicator data."]
    if packet.claims:
        lines += ["", "## Evidence cited in the report", ""]
        for c in packet.claims:
            lines.append(f"- `[c:{c['id']}]` {c['subject']}: {c['predicate']} = {c['value']} {c['unit'] or ''} — \"{c['excerpt'][:160]}\" "
                         f"({c['title'] or c['url']}, {c['origin']}, observed {c['observed']}) [c:{c['id']}]")
    path = ctx.out_dir / "intel" / f"{ctx.day.isoformat()}-{region_id}.md"
    _cleaned, dropped = finalize(ctx, "\n".join(lines), path)
    return RenderResult(path=path, dropped=dropped, stats={"claims_in_packet": len(packet.claims), "indicators": len(packet.indicators),
                                                           "theses": len(rows), "run_id": res.run_id, "dropped": len(dropped)})

"""Readable briefing page: both regions' intelligence reports on one HTML page for a non-technical reader.

The page is rendered from the published Markdown reports (every sentence already cited) plus the ledger: thesis
cards with confidence meters and plain-language falsifiers, key-indicator tiles with trend lines, and the
citation tags turned into numbered footnotes that link to the source excerpt. Nothing on the page is written
here; it only re-presents what the editor role and the ledger produced."""

from __future__ import annotations

import html as H
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import psycopg

from argusvue.settings import get_settings

TAG_RE = re.compile(r"\[c:(\d+)\]|\[i:([a-z0-9_]+):(\d{4}-\d{2}(?:-\d{2})?)\]")
SECTION_ORDER = ["What moved", "Signals", "Risks and contradictions", "Watchlist"]
REGION_SLUG = {"phx": "phoenix", "jh": "jackson"}

THESIS_NAMES = {
    "phx-tsmc-corridor": "TSMC corridor housing", "phx-datacenter-power": "Data centers, siting and power",
    "phx-water-lots": "Water limits and new lots", "phx-apartment-oversupply": "Apartment oversupply and rents",
    "phx-pricecut-lead": "Price cuts as an early signal", "phx-migration-slowdown": "Migration slowdown",
    "jh-federal-land-wealth": "Federal land and wealth pricing", "jh-workforce-shed": "The commuter workforce shed",
    "jh-str-lodging-lead": "Lodging tax as a lead", "jh-airport-gtnp-lead": "Visitation, air service and tax receipts",
    "jh-property-tax-relief": "Property-tax relief and local revenue",
}
TILES = {
    "phx": [("zhvi", "cbsa:38060", "Typical home value (metro)"), ("median_sale_price", "cbsa:38060", "Median sale price (metro)"),
            ("homes_sold", "cbsa:38060", "Homes sold (metro)"), ("active_inventory", "cbsa:38060", "Homes for sale (metro)"),
            ("months_of_supply", "cbsa:38060", "Months of supply (metro)"), ("price_cut_share", "cbsa:38060", "Listings with a price cut"),
            ("zori_rent", "cbsa:38060", "Zillow rent index (metro)"), ("aptlist_rent_overall", "cbsa:38060", "Apartment List rent (metro)"),
            ("permits_total_units", "county:04013", "Housing units permitted (Maricopa)"), ("nonfarm_jobs_total", "cbsa:38060", "Nonfarm jobs (metro)"),
            ("unemployment_rate", "cbsa:38060", "Unemployment rate (metro)"), ("mortgage_rate_30y", "country:US", "30-year mortgage rate (US)")],
    "jh": [("sales_use_tax_distribution", "county:56039", "Sales and use tax received (Teton)"), ("lodging_tax_distribution", "county:56039", "Lodging tax received (Teton)"),
           ("zhvi", "cbsa:27220", "Typical home value (metro)"), ("median_sale_price", "county:56039", "Median sale price (Teton)"),
           ("homes_sold", "county:56039", "Homes sold (Teton)"), ("active_inventory", "county:56039", "Homes for sale (Teton)"),
           ("months_of_supply", "county:56039", "Months of supply (Teton)"), ("median_dom", "county:56039", "Days on market (Teton)"),
           ("zori_rent", "cbsa:27220", "Zillow rent index (metro)"), ("permits_total_units", "county:56039", "Housing units permitted (Teton)"),
           ("unemployment_rate", "county:56039", "Unemployment rate (Teton)"), ("snotel_swe_grand_targhee", "county:56039", "Snowpack, Grand Targhee")],
}
PLAIN_NAMES = {
    "zhvi": "the typical home value", "zori_rent": "the Zillow rent index", "aptlist_rent_overall": "the Apartment List rent estimate",
    "median_sale_price": "the median sale price", "months_of_supply": "months of supply", "homes_sold": "monthly home sales",
    "permits_sf_units": "single-family permits", "permits_total_units": "housing units permitted", "net_migration": "net migration",
    "nonfarm_jobs_construction": "construction employment", "nonfarm_jobs_manufacturing": "manufacturing employment",
    "sales_use_tax_distribution": "the sales and use tax distribution", "lodging_tax_distribution": "the lodging tax distribution",
    "grte_rec_visits": "Grand Teton visits", "qcew_employment_total": "total covered employment", "unemployment_rate": "the unemployment rate",
    "price_cut_share": "the share of listings with a price cut", "active_inventory": "homes for sale", "median_dom": "median days on market",
}
OP_WORDS = {"<": "below", ">": "above", "<=": "at or below", ">=": "at or above"}
ACTION_WORDS = {"downgrade": "We lower our confidence", "retire": "We retire the thesis", "review": "We take another look"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---- the published report ---------------------------------------------------------------------------------

@dataclass
class Report:
    title: str = ""
    headline: str = ""
    day: str = ""
    bottom_line: str = ""
    sections: dict[str, list[str]] = field(default_factory=dict)
    note: str = ""
    indicator_rows: list[list[str]] = field(default_factory=list)
    claim_ids: list[int] = field(default_factory=list)


def parse_intel_markdown(text: str) -> Report:
    """Pull the narrative, note, indicator table and cited claim ids out of an `out/intel/<date>-<region>.md` file."""
    r = Report()
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("# "):
            r.title = line[2:].strip()
            m = re.search(r"(\d{4}-\d{2}-\d{2})$", r.title)
            r.day = m.group(1) if m else ""
            continue
        if line.startswith("## "):
            current = line[3:].strip()
            r.sections.setdefault(current, [])
            continue
        if current is None:
            if line.startswith("> **") and line.endswith("**"):
                r.headline = line[4:-2].strip()
            continue
        if current == "Bottom line":
            if line.strip():
                r.bottom_line = (r.bottom_line + " " + line.strip()).strip()
        elif current == "Note for the operator":
            if line.startswith("> "):
                r.note = (r.note + " " + line[2:].strip()).strip()
        elif current == "Key indicators":
            if line.startswith("|") and not line.startswith("|---") and not line.startswith("| indicator"):
                r.indicator_rows.append([c.strip() for c in line.strip("|").split("|")])
        elif current == "Evidence cited in the report":
            m = re.match(r"- `\[c:(\d+)\]`", line)
            if m:
                r.claim_ids.append(int(m.group(1)))
        elif line.startswith("- "):
            r.sections[current].append(line[2:].strip())
    return r


# ---- citations as numbered footnotes ------------------------------------------------------------------

@dataclass
class Source:
    n: int
    key: str                 # "c:145229" or "i:zhvi:2026-09"
    label: str               # one line for the list
    tip: str                 # tooltip text
    url: str | None = None
    detail: str = ""         # excerpt or indicator values


class Citer:
    """Numbers citation tags in order of first appearance and renders them as footnote links."""

    def __init__(self, region_slug: str, resolve):
        self.region_slug, self.resolve = region_slug, resolve
        self.sources: list[Source] = []
        self.by_key: dict[str, Source] = {}

    def number(self, key: str) -> Source:
        if key not in self.by_key:
            label, tip, url, detail = self.resolve(key)
            s = Source(len(self.sources) + 1, key, label, tip, url, detail)
            self.sources.append(s)
            self.by_key[key] = s
        return self.by_key[key]

    def render(self, text: str) -> str:
        """Escape the sentence and replace each run of tags with superscript footnote numbers."""
        out, pos, run = [], 0, []

        def flush():
            if run:
                chips = "".join(f'<a class="cite" href="#{self.region_slug}-src-{s.n}" data-tip="{H.escape(s.tip, quote=True)}">{s.n}</a>' for s in run)
                out.append(f'<sup class="cites">{chips}</sup>')
                run.clear()

        for m in TAG_RE.finditer(text):
            between = text[pos:m.start()]
            if between.strip():
                flush()
                out.append(H.escape(between.rstrip()))   # no space between a word and its footnote numbers
            key = f"c:{m.group(1)}" if m.group(1) else f"i:{m.group(2)}:{m.group(3)}"
            run.append(self.number(key))
            pos = m.end()
        flush()
        tail = text[pos:]
        out.append(H.escape(tail))
        s = "".join(out)
        return re.sub(r'(<sup class="cites">.*?</sup>)(\.|,|;)', r"\2\1", s)   # footnote numbers follow the punctuation


# ---- formatting ---------------------------------------------------------------------------------------

def fmt_value(v: float | None, unit: str | None) -> str:
    if v is None:
        return "n/a"
    u = (unit or "").lower()
    if u == "pct":
        return f"{v:.1f}%" if abs(v) >= 1 or v == 0 else f"{v * 100:.1f}%"
    if u == "thousand_jobs":
        return f"{v * 1000:,.0f}"
    if u == "thousand_usd":
        return "$" + compact(v * 1000)
    if u == "usd":
        return "$" + compact(v)
    if u == "months":
        return f"{v:.1f} mo"
    if u == "days":
        return f"{v:,.0f} days"
    if u == "inches":
        return f"{v:.1f} in"
    return compact(v)


def compact(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 100_000:
        return f"{v / 1e3:,.0f}K"
    if a >= 1000:
        return f"{v:,.0f}"
    if float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def fmt_period(p: date, frequency: str | None) -> str:
    if frequency in ("daily", "weekly"):
        return f"{MONTHS[p.month - 1]} {p.day}, {p.year}"
    if frequency in ("annual", "yearly"):
        return str(p.year)
    return f"{MONTHS[p.month - 1]} {p.year}"


def sparkline(values: list[float], *, w: int = 160, h: int = 44, pad: int = 5) -> str:
    """Single-series trend line: 2px line, 10% area wash, emphasised end point with a surface ring."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    xs = [pad + (w - 2 * pad) * (i / (n - 1) if n > 1 else 0.5) for i in range(n)]
    ys = [h - pad - (h - 2 * pad) * ((v - lo) / span) for v in values]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))
    area = f"M{xs[0]:.1f},{h - pad} L{pts.replace(' ', ' L')} L{xs[-1]:.1f},{h - pad} Z"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<path class="spark-area" d="{area}"/><polyline class="spark-line" points="{pts}"/>'
            f'<circle class="spark-dot" cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4"/></svg>')


def plain_indicator(indicator_id: str, name: str) -> str:
    return PLAIN_NAMES.get(indicator_id) or re.sub(r"\s*\([^)]*\)", "", name).replace(", NSA", "").strip()


def falsifier_sentence(f: dict, indicator_name: str, geo_name: str, unit: str | None) -> str:
    when = f" in {fmt_period(f['period'], None)}" if f.get("period") else ""
    return (f"{ACTION_WORDS.get(f['action'], 'We re-review')} if {indicator_name} in {geo_name} is "
            f"{OP_WORDS.get(f['operator'], f['operator'])} {fmt_value(float(f['threshold']), unit)}{when}, by {fmt_period(f['by_date'], 'daily')}.")


# ---- ledger queries ------------------------------------------------------------------------------------

def _region_geos(conn, region_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT cbsa_code, county_fips FROM regions WHERE id = %s", (region_id,))
        r = cur.fetchone()
    return [f"cbsa:{r['cbsa_code']}"] + [f"county:{c}" for c in r["county_fips"]] + ["country:US"]


def _geo_names(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT geo_key, name FROM geo_units")
        names = {r["geo_key"]: r["name"] for r in cur.fetchall()}
    names.setdefault("country:US", "United States")
    return {k: (f"ZIP {k.split(':')[1]}" if k.startswith("zcta:") else v) for k, v in names.items()}


def _indicator_meta(conn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, unit, direction_good, frequency FROM indicators")
        return {r["id"]: dict(r) for r in cur.fetchall()}


def _claims(conn, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.predicate, e.canonical_name AS subject, coalesce(c.value_text, c.value_num::text) AS value, c.unit, c.excerpt,
                      d.title, d.url, coalesce(d.origin_key, s.origin_key) AS origin, c.observed_at::date AS observed, c.valid_from
               FROM claims c JOIN entities e ON e.id = c.subject_entity JOIN documents d ON d.id = c.document_id
               LEFT JOIN sources s ON s.id = d.source_id WHERE c.id = ANY(%s)""", (ids,))
        return {r["id"]: dict(r) for r in cur.fetchall()}


def _indicator_at(conn, indicator_id: str, period: str, geos: list[str]) -> list[dict]:
    p = date.fromisoformat(period if len(period) == 10 else period + "-01")
    with conn.cursor() as cur:
        cur.execute("""SELECT geo_key, value FROM indicator_latest WHERE indicator_id = %s AND period = %s AND geo_key = ANY(%s)
                       ORDER BY array_position(%s::text[], geo_key)""", (indicator_id, p, geos, geos))
        return [dict(r) for r in cur.fetchall()]


def _series(conn, indicator_id: str, geo_key: str, day: date, n: int = 24) -> list[tuple[date, float]]:
    with conn.cursor() as cur:
        cur.execute("""SELECT period, value FROM indicator_latest WHERE indicator_id = %s AND geo_key = %s AND period <= %s
                       ORDER BY period DESC LIMIT %s""", (indicator_id, geo_key, day, n))
        rows = cur.fetchall()
    return [(r["period"], float(r["value"])) for r in reversed(rows)]


def _theses(conn, region_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""SELECT t.id, t.slug, t.geo_key, t.domain, t.human_status, v.version, v.statement, v.status, v.confidence,
                              v.change_summary, v.critic_verdict, v.known_disputes, v.created_at,
                              (SELECT e.payload->>'verdict' FROM thesis_events e WHERE e.thesis_id = t.id AND e.kind = 'gate'
                                 ORDER BY e.id DESC LIMIT 1) AS last_gate
                       FROM theses t JOIN thesis_versions v ON v.thesis_id = t.id AND v.version = t.current_version
                       WHERE t.region_id = %s AND t.active ORDER BY v.confidence DESC, t.id""", (region_id,))
        rows = [dict(r) for r in cur.fetchall()]
        for t in rows:
            cur.execute("""SELECT f.*, (SELECT json_build_object('period', c.period, 'value', c.value) FROM falsifier_checks c
                                        WHERE c.falsifier_id = f.id ORDER BY c.checked_at DESC LIMIT 1) AS last_check
                           FROM falsifiers f WHERE f.thesis_id = %s AND f.version = %s ORDER BY f.id""", (t["id"], t["version"]))
            t["falsifiers"] = [dict(f) for f in cur.fetchall()]
    return rows


# ---- page assembly -------------------------------------------------------------------------------------

def _yoy(series: list[tuple[date, float]], frequency: str | None) -> float | None:
    if len(series) < 2:
        return None
    last_p, last_v = series[-1]
    if frequency in ("annual", "yearly"):
        prev = series[-2][1]
    else:
        target = date(last_p.year - 1, last_p.month, 1)
        cands = [v for p, v in series if p.year == target.year and p.month == target.month]
        if not cands:
            return None
        prev = cands[0]
    return None if prev == 0 else (last_v - prev) / abs(prev) * 100


def _tile(conn, region_id: str, indicator_id: str, geo_key: str, label: str, meta: dict, day: date) -> str:
    m = meta.get(indicator_id, {})
    series = _series(conn, indicator_id, geo_key, day)
    if not series:
        return ""
    last_p, last_v = series[-1]
    yoy = _yoy(series, m.get("frequency"))
    good = m.get("direction_good")
    cls = "flat"
    if yoy is not None and abs(yoy) >= 0.05 and good in ("up", "down"):
        cls = "good" if (yoy > 0) == (good == "up") else "bad"
    delta = "" if yoy is None else f'<span class="delta {cls}">{"▲" if yoy > 0 else "▼" if yoy < 0 else "•"} {abs(yoy):.1f}% vs a year earlier</span>'
    return (f'<div class="tile"><div class="tile-label">{H.escape(label)}</div>'
            f'<div class="tile-value">{H.escape(fmt_value(last_v, m.get("unit")))}</div>'
            f'<div class="tile-meta"><span class="muted">{fmt_period(last_p, m.get("frequency"))}</span>{delta}</div>'
            f'{sparkline([v for _, v in series])}</div>')


def _thesis_card(t: dict, meta: dict, geos: dict[str, str], region_slug: str) -> str:
    conf = float(t["confidence"])
    name = THESIS_NAMES.get(t["slug"], t["slug"].replace("-", " "))
    disputes = t.get("known_disputes") or []
    if t["version"] == 1 and t.get("last_gate") in ("no_change", "unchanged"):
        verdict = '<span class="chip neutral">Reviewed, no change needed</span>'
    elif t["version"] == 1:
        verdict = '<span class="chip neutral">Starting point, not yet reviewed</span>'
    elif t["critic_verdict"] == "approve":
        verdict = '<span class="chip good">✓ Approved by the critic</span>'
    else:
        verdict = f'<span class="chip warn">! Critic objection on record ({len([d for d in disputes if d.get("from") == "critic"]) or 1})</span>'
    fals = []
    for f in t["falsifiers"]:
        m = meta.get(f["indicator_id"], {})
        sent = falsifier_sentence(f, plain_indicator(f["indicator_id"], m.get("name", f["indicator_id"])), geos.get(f["geo_key"], f["geo_key"]), m.get("unit"))
        lc = f.get("last_check")
        latest = f' <span class="muted">Latest: {H.escape(fmt_value(float(lc["value"]), m.get("unit")))} ({H.escape(str(lc["period"])[:7])}).</span>' if lc else ""
        fals.append(f"<li>{H.escape(sent)}{latest}</li>")
    change = H.escape(" ".join((t.get("change_summary") or "").split()))
    return (f'<article class="thesis" id="{region_slug}-thesis-{t["id"]}">'
            f'<header><div class="thesis-kicker"><span class="domain">{H.escape(t["domain"])}</span>'
            f'<span class="muted">{H.escape(geos.get(t["geo_key"], t["geo_key"]))} · version {t["version"]}</span></div>'
            f'<h3>{H.escape(name)}</h3></header>'
            f'<div class="meter" role="img" aria-label="confidence {conf:.2f} of 1"><div class="meter-fill" style="width:{conf * 100:.0f}%"></div></div>'
            f'<div class="meter-row"><span><strong>{conf:.2f}</strong> confidence · {H.escape(t["status"])}</span>{verdict}</div>'
            f'<p class="statement">{H.escape(t["statement"])}</p>'
            f'<h4>What would change our mind</h4><ul class="fals">{"".join(fals) or "<li>No open test yet.</li>"}</ul>'
            f'<details><summary>Why this version changed</summary><p>{change or "Seed version."}</p></details>'
            f'</article>')


def _resolver(conn, claims: dict[int, dict], meta: dict, geos: dict[str, str], region_geos: list[str]):
    def resolve(key: str) -> tuple[str, str, str | None, str]:
        if key.startswith("c:"):
            c = claims.get(int(key[2:]))
            if not c:
                return f"Claim {key[2:]}", "Claim in the ledger", None, ""
            src = c["title"] or c["url"] or c["origin"] or "source"
            label = f"{src} ({c['origin']}, read {c['observed']})"
            excerpt = " ".join((c["excerpt"] or "").split())
            tip = f"“{excerpt[:220]}{'…' if len(excerpt) > 220 else ''}” — {src}"
            return label, tip, c["url"], excerpt
        _, ind, period = key.split(":", 2)
        m = meta.get(ind, {})
        rows = _indicator_at(conn, ind, period, region_geos)
        vals = "; ".join(f"{geos.get(r['geo_key'], r['geo_key'])}: {fmt_value(float(r['value']), m.get('unit'))}" for r in rows)
        label = f"{m.get('name', ind)}, {period}"
        return label, f"{label}. {vals}", None, vals
    return resolve


def _region_block(conn, region_id: str, report: Report, meta: dict, geos: dict[str, str], day: date) -> str:
    slug = REGION_SLUG.get(region_id, region_id)
    region_geos = _region_geos(conn, region_id)
    claims = _claims(conn, sorted(set(report.claim_ids) | {int(m.group(1)) for m in TAG_RE.finditer(report.bottom_line + " ".join(x for items in report.sections.values() for x in items)) if m.group(1)}))
    citer = Citer(slug, _resolver(conn, claims, meta, geos, region_geos))
    lede = citer.render(report.bottom_line)
    sections_html = []
    for name in SECTION_ORDER:
        items = report.sections.get(name, [])
        if not items:
            continue
        sections_html.append(f'<section class="block"><h2>{H.escape(name)}</h2><ul class="items">' +
                             "".join(f"<li>{citer.render(i)}</li>" for i in items) + "</ul></section>")
    tiles = "".join(_tile(conn, region_id, i, g, lbl, meta, day) for i, g, lbl in TILES.get(region_id, []))
    theses = _theses(conn, region_id)
    cards = "".join(_thesis_card(t, meta, geos, slug) for t in theses)
    confs = [float(t["confidence"]) for t in theses]
    thesis_intro = (f"{len(theses)} working theses, confidence from {min(confs):.2f} to {max(confs):.2f} of 1. "
                    "Confidence is the system's own estimate that the statement is right; below 0.5 means a hunch with some evidence, not a conclusion.") if confs else ""
    sources = "".join(
        f'<li id="{slug}-src-{s.n}"><span class="src-label">{(f"<a href=\"{H.escape(s.url, quote=True)}\" target=\"_blank\" rel=\"noopener\">{H.escape(s.label)}</a>" if s.url else H.escape(s.label))}</span>'
        f'{(f"<div class=\"src-detail\">“{H.escape(s.detail)}”</div>" if s.detail and s.key.startswith("c:") else (f"<div class=\"src-detail\">{H.escape(s.detail)}</div>" if s.detail else ""))}</li>'
        for s in citer.sources)
    table = ""
    if report.indicator_rows:
        body = "".join("<tr>" + "".join(f"<td>{H.escape(c.strip('`'))}</td>" for c in row[:7]) + "</tr>" for row in report.indicator_rows)
        table = ('<details class="block"><summary>All key indicators (table)</summary><div class="scroll"><table><thead><tr>'
                 '<th>Indicator</th><th>Area</th><th>Period</th><th>Latest</th><th>vs last month</th><th>vs last year</th><th>z</th></tr></thead>'
                 f'<tbody>{body}</tbody></table></div></details>')
    note = f'<aside class="note"><h2>Notes from the system</h2><p>{H.escape(report.note)}</p></aside>' if report.note else ""
    return (f'<section class="region" id="region-{slug}" data-slug="{slug}" data-region="{region_id}">'
            f'<div class="region-head"><p class="eyebrow">{H.escape(report.title.split(":")[0])} · report for {H.escape(report.day)}</p>'
            f'<h1>{H.escape(report.headline)}</h1></div>'
            f'<div class="grid"><div class="main">'
            f'<section class="block lede-block"><h2>Bottom line</h2><p class="lede">{lede}</p></section>'
            f'{"".join(sections_html)}{note}</div>'
            f'<aside class="side"><h2>At a glance</h2><div class="tiles">{tiles}</div></aside></div>'
            f'<section class="block theses-block"><h2>What we think, and what would change our mind</h2><p class="muted">{thesis_intro}</p>'
            f'<div class="cards">{cards}</div></section>{table}'
            f'<section class="block sources-block"><h2>Sources behind the numbers</h2><p class="muted">Each small number in the text points here. Quoted text is taken word for word from the source; indicator lines show the value the sentence used.</p>'
            f'<ol class="sources">{sources}</ol></section></section>')


def render_html(conn: psycopg.Connection, day: date, out_dir: Path | None = None, regions: tuple[str, ...] = ("phx", "jh")) -> Path:
    out_dir = out_dir or get_settings().out_dir
    meta, geos = _indicator_meta(conn), _geo_names(conn)
    blocks, nav = [], []
    for rid in regions:
        path = out_dir / "intel" / f"{day.isoformat()}-{rid}.md"
        if not path.exists():
            continue
        report = parse_intel_markdown(path.read_text(encoding="utf-8"))
        blocks.append(_region_block(conn, rid, report, meta, geos, day))
        nav.append((REGION_SLUG.get(rid, rid), report.title.split(":")[0]))
    if not blocks:
        raise FileNotFoundError(f"no intel reports for {day} in {out_dir / 'intel'}")
    buttons = "".join(f'<a class="seg" href="#{slug}" data-target="{slug}">{H.escape(name)}</a>' for slug, name in nav)
    page = TEMPLATE.replace("{{DATE}}", H.escape(fmt_period(day, "daily"))).replace("{{NAV}}", buttons).replace("{{BLOCKS}}", "".join(blocks)) \
        .replace("{{DEFAULT}}", nav[0][0])
    out = out_dir / "intel" / "index.html"
    out.write_text(page, encoding="utf-8")
    return out


TEMPLATE = r"""<title>Argusvue Briefing</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=Public+Sans:wght@400;500;600;700&display=swap">
<style>
/* Layout: a briefing memo. One narrative column with a side rail of trend tiles; theses and sources below. */
:root {
  --bg: #f4f6f8; --surface: #ffffff; --surface-2: #eceff3; --ink: #182230; --ink-2: #4b5666; --muted: #7b8595; --hair: #dfe4ea;
  --accent: #1f5f8b; --accent-soft: #e4eef6; --series: #2a78d6; --series-wash: rgba(42,120,214,0.10);
  --good: #0f7a3a; --good-soft: #e3f3ea; --warn: #9a5b00; --warn-soft: #fbf0d9; --bad: #b3362f;
  --display: "Newsreader", Georgia, "Times New Roman", serif; --sans: "Public Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #111417; --surface: #1a1e24; --surface-2: #232931; --ink: #e9ecf0; --ink-2: #b7bfc9; --muted: #8b95a3; --hair: #2b323b;
  --accent: #7db3dc; --accent-soft: #1d2b38; --series: #3987e5; --series-wash: rgba(57,135,229,0.14);
  --good: #4fc27a; --good-soft: #15301f; --warn: #e2a84a; --warn-soft: #3a2c13; --bad: #e07a73; color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #111417; --surface: #1a1e24; --surface-2: #232931; --ink: #e9ecf0; --ink-2: #b7bfc9; --muted: #8b95a3; --hair: #2b323b;
  --accent: #7db3dc; --accent-soft: #1d2b38; --series: #3987e5; --series-wash: rgba(57,135,229,0.14);
  --good: #4fc27a; --good-soft: #15301f; --warn: #e2a84a; --warn-soft: #3a2c13; --bad: #e07a73; color-scheme: dark; }

* { box-sizing: border-box; }
body { background: var(--bg); color: var(--ink); font-family: var(--sans); font-size: 16px; line-height: 1.55; margin: 0; }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 10; background: var(--surface); }
a { color: var(--accent); }
.topbar { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--surface); border-bottom: 1px solid var(--hair);
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 10px; }
.topbar-in { max-width: 1180px; margin: 0 auto; display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: center; justify-content: space-between; }
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand b { font-family: var(--display); font-weight: 600; font-size: 1.35rem; letter-spacing: -0.01em; }
.brand span { color: var(--muted); font-size: 0.9rem; }
.segs { display: inline-flex; border: 1px solid var(--hair); border-radius: 999px; padding: 3px; background: var(--surface-2); }
.seg { padding: 6px 14px; border-radius: 999px; text-decoration: none; color: var(--ink-2); font-weight: 600; font-size: 0.92rem; }
.seg[aria-current="true"] { background: var(--surface); color: var(--ink); box-shadow: 0 1px 2px rgba(0,0,0,0.08); }
.seg:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.wrap { max-width: 1180px; margin: 0 auto; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 22px 56px; }
.howto { background: var(--accent-soft); border-radius: 10px; padding: 12px 16px; font-size: 0.95rem; color: var(--ink-2); margin-bottom: 26px; max-width: 70ch; }
.howto b { color: var(--ink); }
.region[hidden] { display: none; }
.region, .sources li, .thesis { scroll-margin-top: 76px; }
.items li, .lede, .statement, .fals, .note, .sources, .src-detail, .tile-label { overflow-wrap: anywhere; }
.src-label a { word-break: break-word; }
.eyebrow { text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.74rem; font-weight: 600; color: var(--muted); margin: 0 0 6px; }
h1 { font-family: var(--display); font-weight: 500; font-size: clamp(1.7rem, 1.2rem + 2vw, 2.6rem); line-height: 1.15; letter-spacing: -0.01em; margin: 0 0 22px; text-wrap: balance; max-width: 26ch; }
h2 { font-family: var(--display); font-weight: 600; font-size: 1.3rem; margin: 0 0 10px; letter-spacing: -0.005em; }
h3 { font-family: var(--display); font-weight: 600; font-size: 1.2rem; margin: 2px 0 8px; line-height: 1.25; }
h4 { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); margin: 14px 0 6px; }
.grid { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 28px 40px; align-items: start; }
.main, .side { min-width: 0; }
.block { margin-bottom: 26px; }
.lede { font-family: var(--display); font-size: 1.22rem; line-height: 1.5; margin: 0; max-width: 62ch; }
.items { list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; max-width: 70ch; }
.items li { padding: 10px 14px; background: var(--surface); border: 1px solid var(--hair); border-radius: 8px; }
.sources-block .items li, .lede-block { background: none; }
.cites { line-height: 0; margin-left: 2px; }
.cite { display: inline-block; font-family: var(--mono); font-size: 0.68rem; font-weight: 600; text-decoration: none; color: var(--accent);
  background: var(--accent-soft); border-radius: 4px; padding: 1px 4px; margin-left: 2px; position: relative; vertical-align: super; line-height: 1.1; }
.cite:hover, .cite:focus-visible { background: var(--accent); color: #fff; outline: none; }
.cite:hover::after, .cite:focus-visible::after { content: attr(data-tip); position: absolute; left: 0; top: calc(100% + 6px); z-index: 6; width: min(340px, 78vw);
  background: var(--ink); color: var(--bg); font-family: var(--sans); font-weight: 400; font-size: 0.8rem; line-height: 1.4; padding: 8px 10px; border-radius: 6px;
  box-shadow: 0 6px 20px rgba(0,0,0,0.18); white-space: normal; vertical-align: baseline; }
.note { background: var(--surface-2); border-radius: 10px; padding: 14px 16px; font-size: 0.93rem; color: var(--ink-2); max-width: 70ch; }
.note h2 { font-size: 1.05rem; }
.side h2 { font-size: 1.1rem; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
.tile { background: var(--surface); border: 1px solid var(--hair); border-radius: 8px; padding: 10px 12px 6px; min-width: 0; }
.tile-label { font-size: 0.76rem; color: var(--ink-2); line-height: 1.3; min-height: 2.6em; }
.tile-value { font-size: 1.35rem; font-weight: 600; letter-spacing: -0.01em; margin-top: 2px; }
.tile-meta { display: flex; flex-direction: column; gap: 1px; font-size: 0.72rem; margin: 2px 0 4px; }
.delta { font-weight: 600; color: var(--ink-2); }
.delta.good { color: var(--good); } .delta.bad { color: var(--bad); }
.muted { color: var(--muted); }
.spark { display: block; width: 100%; height: 44px; }
.spark-line { fill: none; stroke: var(--series); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.spark-area { fill: var(--series-wash); stroke: none; }
.spark-dot { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 14px; margin-top: 12px; }
.thesis { background: var(--surface); border: 1px solid var(--hair); border-radius: 10px; padding: 16px 18px; min-width: 0; display: flex; flex-direction: column; }
.thesis-kicker { display: flex; justify-content: space-between; gap: 8px; font-size: 0.74rem; flex-wrap: wrap; }
.domain { text-transform: uppercase; letter-spacing: 0.07em; font-weight: 700; color: var(--accent); }
.meter { height: 8px; border-radius: 999px; background: var(--series-wash); overflow: hidden; margin: 4px 0 6px; }
.meter-fill { height: 100%; background: var(--series); border-radius: 999px; }
.meter-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 0.85rem; color: var(--ink-2); }
.chip { font-size: 0.74rem; font-weight: 600; padding: 2px 8px; border-radius: 999px; background: var(--surface-2); color: var(--ink-2); white-space: nowrap; }
.chip.good { background: var(--good-soft); color: var(--good); } .chip.warn { background: var(--warn-soft); color: var(--warn); }
.statement { margin: 12px 0 0; font-size: 0.97rem; }
.fals { margin: 0; padding-left: 18px; font-size: 0.88rem; color: var(--ink-2); display: grid; gap: 6px; }
details { margin-top: 12px; font-size: 0.88rem; color: var(--ink-2); }
summary { cursor: pointer; color: var(--accent); font-weight: 600; }
summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.scroll { overflow-x: auto; margin-top: 10px; }
table { border-collapse: collapse; font-size: 0.85rem; min-width: 640px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--hair); white-space: nowrap; }
th { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
td:nth-child(n+4), th:nth-child(n+4) { text-align: right; }
.sources { padding-left: 1.6em; font-size: 0.88rem; display: grid; gap: 8px; color: var(--ink-2); max-width: 80ch; }
.sources li:target { background: var(--accent-soft); border-radius: 6px; padding: 4px 6px; margin-left: -6px; }
.src-detail { color: var(--muted); font-size: 0.84rem; margin-top: 2px; }
footer { color: var(--muted); font-size: 0.82rem; margin-top: 40px; border-top: 1px solid var(--hair); padding-top: 14px; }
@media (max-width: 880px) { .grid { grid-template-columns: minmax(0, 1fr); } .cards { grid-template-columns: minmax(0, 1fr); } }
@media (prefers-reduced-motion: no-preference) { .meter-fill { transition: width 400ms ease; } }
</style>
<header class="topbar"><div class="topbar-in">
  <div class="brand"><b>Argusvue</b><span>Market intelligence briefing · {{DATE}}</span></div>
  <nav class="segs" aria-label="Region">{{NAV}}</nav>
</div></header>
<main class="wrap">
  <div class="howto"><b>How to read this.</b> Each statement ends with small numbered tags; hover or tap one to see the exact source sentence or data point, or jump to the list at the bottom. "May" is deliberate: the system reports what the evidence supports and no more. Confidence meters are the system's own estimate, where 1.0 would be certainty.</div>
  {{BLOCKS}}
  <footer>Generated by Argusvue from its evidence ledger. Every sentence above carries the ids of the claims or indicator points behind it; sentences without evidence were removed before publication.</footer>
</main>
<script>
(function () {
  var regions = Array.prototype.slice.call(document.querySelectorAll('.region'));
  var segs = Array.prototype.slice.call(document.querySelectorAll('.seg'));
  var fallback = '{{DEFAULT}}';
  function slugOf(r) { return r.getAttribute('data-slug'); }
  function show() {
    var want = (location.hash || '').replace('#', '');
    var target = null;
    regions.forEach(function (r) { if (slugOf(r) === want) target = r; });
    if (!target) {   // a source anchor inside a region, or no hash
      regions.forEach(function (r) { if (!target && want && r.querySelector('#' + CSS.escape(want))) target = r; });
    }
    if (!target) { regions.forEach(function (r) { if (slugOf(r) === fallback) target = r; }); }
    regions.forEach(function (r) { r.hidden = (r !== target); });
    segs.forEach(function (s) { s.setAttribute('aria-current', s.getAttribute('data-target') === slugOf(target) ? 'true' : 'false'); });
    if (want && want !== slugOf(target)) { var el = document.getElementById(want); if (el) { el.scrollIntoView({block: 'center'}); } }
    else { window.scrollTo(0, 0); }
  }
  window.addEventListener('hashchange', show);
  show();
})();
</script>
"""

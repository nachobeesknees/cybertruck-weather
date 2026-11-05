"""Shared rendering helpers: bi-temporal cutoff, thesis selection, indicator series, tag validation."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from argusvue.quant.anomalies import series_stats
from argusvue.render.tags import Tag, split_sentences
from argusvue.settings import get_settings

log = logging.getLogger(__name__)
MAX_TAGS = 6
STATUS_ORDER = ("established", "emerging", "weakening", "dead")


@dataclass
class Ctx:
    conn: psycopg.Connection
    region_id: str
    region_name: str
    day: date                    # the local day being rendered
    as_of: date | None
    cutoff: datetime | None      # observed-at upper bound (end of `day` local) when as_of is given, else None
    win_end: datetime            # end of the "since yesterday" window
    tz: ZoneInfo
    out_dir: Path
    stats: dict = field(default_factory=dict)

    @property
    def win_start(self) -> datetime:
        return self.win_end - timedelta(hours=24)


def make_ctx(conn: psycopg.Connection, region_id: str, as_of: date | None, out_dir: Path | None) -> Ctx:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM regions WHERE id = %s", (region_id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown region {region_id!r}")
    cutoff = datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=tz) if as_of else None
    day = as_of or datetime.now(tz).date()
    return Ctx(conn, region_id, row["name"], day, as_of, cutoff, cutoff or datetime.now(tz), tz, out_dir or s.out_dir)


# ---- formatting ----------------------------------------------------------------------------------------

def one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def fmt_num(v: float | None) -> str:
    if v is None:
        return ""
    s = f"{v:,.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_pct(v: float | None) -> str:
    return "" if v is None else f"{v:+.1f}%"


def fmt_z(v: float | None) -> str:
    return "" if v is None else f"{v:+.2f}"


def point_tag(indicator_id: str, period: date, frequency: str | None) -> Tag:
    daily = frequency in ("daily", "weekly")
    return Tag("i", indicator_id, period.isoformat() if daily else period.strftime("%Y-%m"))


def tag_str(tags: list[Tag], limit: int = MAX_TAGS) -> str:
    return " ".join(str(t) for t in tags[:limit])


def attribute(text: str, tags: list[Tag], *, first_only: bool = False) -> str:
    """Append the tags before the terminal punctuation of each sentence (or only the first one)."""
    ts = tag_str(tags)
    out = []
    for n, sent in enumerate(split_sentences(one_line(text))):
        if (first_only and n > 0) or not ts:
            out.append(sent)
            continue
        end = sent[-1] if sent[-1] in ".!?" else "."
        body = sent[:-1] if sent[-1] in ".!?" else sent
        out.append(f"{body} {ts}{end}")
    return " ".join(out)


def defuse(text: str | None) -> str:
    """Free text inside a cited sentence must not look like a sentence boundary (it would orphan the tag)."""
    return re.sub(r"[.!?]+\s+", "; ", one_line(text))


def claim_phrase(value_num, value_text, unit) -> str:
    if value_num is not None:
        return f"{fmt_num(float(value_num))}{(' ' + unit) if unit else ''}"
    return defuse(value_text) or "n/a"


# ---- bi-temporal queries ---------------------------------------------------------------------------------

def thesis_rows(ctx: Ctx, geo_keys: list[str] | None = None) -> list[dict]:
    """Per active thesis: the highest version created at or before the cutoff (never above current_version
    when rendering live, so a human-rejected version is not shown)."""
    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT t.id, t.slug, t.geo_key, t.domain, t.approved_version, t.current_version,
                      v.version, v.statement, v.status, v.confidence, v.change_summary, v.created_at,
                      (v.version > t.approved_version) AS pending_human
               FROM theses t
               JOIN LATERAL (SELECT * FROM thesis_versions v
                             WHERE v.thesis_id = t.id
                               AND (%(cut)s::timestamptz IS NULL OR v.created_at <= %(cut)s)
                               AND (%(cut)s::timestamptz IS NOT NULL OR v.version <= t.current_version)
                               AND NOT EXISTS (SELECT 1 FROM review_decisions d WHERE d.thesis_id = v.thesis_id AND d.version = v.version
                                               AND d.decision = 'rejected' AND %(cut)s::timestamptz IS NOT NULL AND d.decided_at <= %(cut)s)
                             ORDER BY v.version DESC LIMIT 1) v ON true
               WHERE t.active AND t.region_id = %(r)s AND (%(geos)s::text[] IS NULL OR t.geo_key = ANY(%(geos)s))
               ORDER BY t.slug""",
            {"cut": ctx.cutoff, "r": ctx.region_id, "geos": geo_keys},
        )
        return cur.fetchall()


def support_claim_tags(ctx: Ctx, thesis_id: int, version: int) -> list[Tag]:
    with ctx.conn.cursor() as cur:
        cur.execute("SELECT claim_id FROM thesis_claims WHERE thesis_id = %s AND version = %s AND role = 'support' ORDER BY claim_id",
                    (thesis_id, version))
        return [Tag("c", str(r["claim_id"])) for r in cur.fetchall()]


def falsifier_point_tags(ctx: Ctx, thesis_id: int, version: int) -> list[Tag]:
    """Indicator points behind a seed thesis: its falsifiers' latest checks, else the baseline point the threshold was set from."""
    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (f.id) f.indicator_id, c.period, i.frequency
               FROM falsifiers f JOIN falsifier_checks c ON c.falsifier_id = f.id JOIN indicators i ON i.id = f.indicator_id
               WHERE f.thesis_id = %(t)s AND f.version <= %(v)s AND c.period IS NOT NULL
                 AND (%(cut)s::timestamptz IS NULL OR c.checked_at <= %(cut)s)
               ORDER BY f.id, c.checked_at DESC, c.id DESC""",
            {"t": thesis_id, "v": version, "cut": ctx.cutoff},
        )
        rows = cur.fetchall()
        if not rows:
            cur.execute(
                """SELECT f.indicator_id, (f.baseline->>'period')::date AS period, i.frequency
                   FROM falsifiers f JOIN indicators i ON i.id = f.indicator_id
                   WHERE f.thesis_id = %(t)s AND f.version <= %(v)s AND f.baseline ? 'period'
                     AND (%(cut)s::timestamptz IS NULL OR f.created_at <= %(cut)s)
                   ORDER BY f.id""",
                {"t": thesis_id, "v": version, "cut": ctx.cutoff},
            )
            rows = cur.fetchall()
    seen, out = set(), []
    for r in rows:
        t = point_tag(r["indicator_id"], r["period"], r["frequency"])
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def thesis_tags(ctx: Ctx, row: dict) -> list[Tag]:
    return support_claim_tags(ctx, row["id"], row["version"]) or falsifier_point_tags(ctx, row["id"], row["version"])


def thesis_bullet(ctx: Ctx, row: dict) -> str:
    """One bullet per thesis: statement (every sentence cited), confidence line, one-line change summary
    (only its first sentence is attributed; anything beyond is unattributed prose and gets dropped)."""
    tags = thesis_tags(ctx, row)
    head = f"**{row['slug']}** (`{row['geo_key']}`)"
    if not tags:
        return f"> {head}: (no cited evidence yet)"
    pend = ", pending human approval" if row["pending_human"] else ""
    statement = attribute(row["statement"], tags)
    conf = f"Confidence {float(row['confidence']):.2f}, {row['status']}, v{row['version']}{pend} {tag_str(tags)}."
    change = attribute("Latest change: " + one_line(row["change_summary"]), tags, first_only=True)
    return f"- {head}: {statement} {conf} {change}"


def indicator_series(ctx: Ctx, geo_keys: list[str]) -> list[dict]:
    """Latest point per (indicator, geo) as observed by the cutoff (latest revision as of then) with MoM/YoY/z."""
    with ctx.conn.cursor() as cur:
        cur.execute(
            """SELECT p.indicator_id, p.geo_key, p.period, p.value, i.name, i.unit, i.frequency
               FROM (SELECT DISTINCT ON (indicator_id, geo_key, period) indicator_id, geo_key, period, value
                     FROM indicator_points
                     WHERE geo_key = ANY(%(g)s) AND (%(cut)s::timestamptz IS NULL OR observed_at <= %(cut)s)
                     ORDER BY indicator_id, geo_key, period, observed_at DESC) p
               JOIN indicators i ON i.id = p.indicator_id
               ORDER BY p.indicator_id, p.geo_key, p.period""",
            {"g": geo_keys, "cut": ctx.cutoff},
        )
        rows = cur.fetchall()
        cur.execute(
            """SELECT DISTINCT ON (indicator_id, geo_key, period) indicator_id, geo_key, period, value, zscore, yoy_pct, mom_pct
               FROM indicator_anomalies
               WHERE geo_key = ANY(%(g)s) AND (%(cut)s::timestamptz IS NULL OR computed_at <= %(cut)s)
               ORDER BY indicator_id, geo_key, period, computed_at DESC""",
            {"g": geo_keys, "cut": ctx.cutoff},
        )
        anomalies = {(a["indicator_id"], a["geo_key"], a["period"]): a for a in cur.fetchall()}
    series: dict[tuple[str, str], list] = {}
    meta: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (r["indicator_id"], r["geo_key"])
        series.setdefault(k, []).append((r["period"], float(r["value"])))
        meta[k] = r
    out = []
    for k, pts in series.items():
        m = meta[k]
        period, value = pts[-1]
        a = anomalies.get((k[0], k[1], period))
        if a is not None and abs(float(a["value"]) - value) < 1e-9:
            mom, yoy, z = (None if a[c] is None else float(a[c]) for c in ("mom_pct", "yoy_pct", "zscore"))
        else:
            st = series_stats(pts, m["frequency"], 1)[0]
            mom, yoy, z = st["mom_pct"], st["yoy_pct"], st["zscore"]
        out.append({"indicator_id": k[0], "geo_key": k[1], "name": m["name"], "unit": m["unit"], "frequency": m["frequency"],
                    "period": period, "value": value, "mom": mom, "yoy": yoy, "z": z, "tag": point_tag(k[0], period, m["frequency"])})
    return out


def indicator_table(rows: list[dict], *, with_geo: bool) -> list[str]:
    head = ["indicator"] + (["geo"] if with_geo else []) + ["period", "latest", "MoM", "YoY", "z", "tag"]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        cells = [r["name"].replace("|", "/")] + ([f"`{r['geo_key']}`"] if with_geo else []) + [
            r["period"].isoformat()[:7] if r["frequency"] not in ("daily", "weekly") else r["period"].isoformat(),
            f"{fmt_num(r['value'])} {r['unit']}".strip(), fmt_pct(r["mom"]), fmt_pct(r["yoy"]), fmt_z(r["z"]), str(r["tag"])]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


# ---- tag validation --------------------------------------------------------------------------------------

def make_validator(ctx: Ctx):
    cache: dict[Tag, bool] = {}

    def valid(tag: Tag) -> bool:
        if tag in cache:
            return cache[tag]
        with ctx.conn.cursor() as cur:
            if tag.kind == "c":
                cur.execute("SELECT 1 FROM claims WHERE id = %s AND (%s::timestamptz IS NULL OR observed_at <= %s)",
                            (int(tag.ident), ctx.cutoff, ctx.cutoff))
            elif tag.kind == "run":
                cur.execute("SELECT 1 FROM agent_runs WHERE id = %s", (int(tag.ident),))
            else:
                try:
                    if len(tag.period or "") == 7:
                        lo = date.fromisoformat(tag.period + "-01")
                        hi = (lo.replace(day=28) + timedelta(days=4)).replace(day=1)
                    else:
                        lo = date.fromisoformat(tag.period or "")
                        hi = lo + timedelta(days=1)
                except ValueError:
                    cache[tag] = False
                    return False
                cur.execute("""SELECT 1 FROM indicator_points WHERE indicator_id = %s AND period >= %s AND period < %s
                               AND (%s::timestamptz IS NULL OR observed_at <= %s) LIMIT 1""",
                            (tag.ident, lo, hi, ctx.cutoff, ctx.cutoff))
            cache[tag] = cur.fetchone() is not None
        return cache[tag]

    return valid


def finalize(ctx: Ctx, markdown: str, path: Path) -> tuple[str, list[str]]:
    from argusvue.render.tags import enforce

    cleaned, dropped = enforce(re.sub(r"\n{3,}", "\n\n", markdown), valid=make_validator(ctx))
    for s in dropped:
        log.warning("render dropped untagged/invalid sentence (%s %s): %s", ctx.region_id, path.name, s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cleaned.rstrip("\n") + "\n", encoding="utf-8")
    return cleaned, dropped

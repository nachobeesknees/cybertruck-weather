"""Build the analyst/critic packet for a thesis from the ledger only (no LLM): current version, delta since the
last review, family theses, leading indicators, and the id allowlist. Bi-temporal: `as_of` limits every query
to what was observed by then (used by `replay`)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

MAX_CLAIMS = 60
EXCERPT_CHARS = 240


@dataclass
class Packet:
    thesis_id: int
    region_id: str
    geo_key: str
    domain: str
    as_of: datetime
    current: dict[str, Any]
    delta: dict[str, Any]
    family: list[dict[str, Any]]
    leading: list[dict[str, Any]]
    claims: dict[int, dict[str, Any]]            # id -> compact claim (the allowlist)
    indicators: dict[str, dict[str, Any]]        # 'indicator_id@geo_key' -> latest points (the allowlist)
    last_reviewed_at: datetime | None
    notes: list[str] = field(default_factory=list)

    @property
    def allowlist_claims(self) -> set[int]:
        return set(self.claims)

    @property
    def allowlist_indicators(self) -> set[str]:
        return set(self.indicators)

    def to_prompt(self) -> str:
        return json.dumps({
            "thesis": self.current, "delta": self.delta, "family": self.family, "leading_indicators": self.leading,
            "claims_allowlist": list(self.claims.values()), "indicators_allowlist": self.indicators,
            "as_of": self.as_of.isoformat(), "last_reviewed_at": self.last_reviewed_at.isoformat() if self.last_reviewed_at else None,
            "notes": self.notes,
        }, default=str, indent=1)


def _compact_claim(r: dict, as_of: datetime) -> dict[str, Any]:
    age = (as_of - r["observed_at"]).total_seconds() / 86400.0
    hl = r["half_life_days"]
    eff = float(r["confidence"]) * (0.5 ** (max(age, 0) / hl) if hl else 1.0)
    return {"id": r["id"], "predicate": r["predicate"], "subject": r["subject"], "value": r["value_text"] if r["value_text"] is not None
            else (float(r["value_num"]) if r["value_num"] is not None else None), "unit": r["unit"], "valid_from": str(r["valid_from"] or ""),
            "geo": r["geo_key"], "origin": r["origin_key"], "status": r["status"], "confidence": round(float(r["confidence"]), 3),
            "effective_confidence": round(eff, 3), "past_half_life": bool(hl and age > hl), "observed": r["observed_at"].date().isoformat(),
            "excerpt": (r["excerpt"] or "")[:EXCERPT_CHARS], "document": r["document_id"]}


_CLAIM_SELECT = """
    SELECT c.id, c.predicate, e.canonical_name AS subject, c.value_num, c.value_text, c.unit, c.valid_from, c.geo_key,
           coalesce(d.origin_key, s.origin_key) AS origin_key, c.status, c.confidence, c.half_life_days, c.observed_at, c.excerpt,
           c.document_id, c.attrs
    FROM claims c JOIN entities e ON e.id = c.subject_entity JOIN documents d ON d.id = c.document_id
    LEFT JOIN sources s ON s.id = d.source_id
"""


def build_packet(conn: psycopg.Connection, thesis_id: int, *, as_of: datetime | None = None) -> Packet:
    as_of = as_of or datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM theses WHERE id = %s", (thesis_id,))
        t = cur.fetchone()
        if t is None:
            raise KeyError(f"thesis {thesis_id} not found")
        cur.execute("""SELECT * FROM thesis_versions WHERE thesis_id = %s AND created_at <= %s ORDER BY version DESC LIMIT 1""",
                    (thesis_id, as_of))
        v = cur.fetchone()
        pre_existence_note = None
        if v is None:   # replaying a date before the thesis existed: use its first statement, evidence as of then
            cur.execute("SELECT * FROM thesis_versions WHERE thesis_id = %s ORDER BY version LIMIT 1", (thesis_id,))
            v = cur.fetchone()
            if v is None:
                raise KeyError(f"thesis {thesis_id} has no versions")
            pre_existence_note = f"thesis did not exist as of {as_of.date()}; replaying its first statement against evidence observed by then"
        cur.execute("SELECT claim_id, role FROM thesis_claims WHERE thesis_id = %s AND version = %s", (thesis_id, v["version"]))
        cited = cur.fetchall()
        cur.execute("""SELECT id, cause, effect, mechanism, lag_months, strength, evidence_claim_ids, cause_indicator, effect_indicator,
                              geo_key, status, test_result FROM causal_edges WHERE thesis_id = %s AND version = %s ORDER BY id""",
                    (thesis_id, v["version"]))
        edges = cur.fetchall()
        cur.execute("""SELECT f.id, f.indicator_id, f.geo_key, f.operator, f.threshold, f.by_date, f.action, f.description, f.outcome,
                              (SELECT json_build_object('period', c.period, 'value', c.value, 'triggered', c.triggered, 'checked_at', c.checked_at)
                                 FROM falsifier_checks c WHERE c.falsifier_id = f.id AND c.checked_at <= %s ORDER BY c.checked_at DESC LIMIT 1) AS last_check
                       FROM falsifiers f WHERE f.thesis_id = %s AND f.version = %s ORDER BY f.id""", (as_of, thesis_id, v["version"]))
        fals = cur.fetchall()
        last_reviewed = t["last_reviewed_at"] if t["last_reviewed_at"] and t["last_reviewed_at"] <= as_of else v["created_at"]
        region_cbsa = None
        cur.execute("SELECT cbsa_code FROM regions WHERE id = %s", (t["region_id"],))
        rr = cur.fetchone()
        region_cbsa = f"cbsa:{rr['cbsa_code']}" if rr else None

        # --- candidate claims: cited ones + recent claims at/below the thesis geo in the domain (or indicator claims)
        claims: dict[int, dict] = {}
        cited_ids = [c["claim_id"] for c in cited]
        if cited_ids:
            cur.execute(_CLAIM_SELECT + " WHERE c.id = ANY(%s)", (cited_ids,))
            for r in cur.fetchall():
                claims[r["id"]] = _compact_claim(r, as_of)
        cur.execute(
            _CLAIM_SELECT + """
            WHERE c.observed_at <= %s AND c.status NOT IN ('retracted','superseded')
              AND c.extractor_version LIKE 'extract@%%'
              AND EXISTS (SELECT 1 FROM claim_geo g WHERE g.claim_id = c.id AND g.geo_key = %s)
              AND (c.attrs->>'domain' = %s OR %s = 'housing' AND c.attrs->>'domain' IN ('housing','policy'))
            ORDER BY c.observed_at DESC LIMIT %s""",
            (as_of, t["geo_key"], t["domain"], t["domain"], MAX_CLAIMS),
        )
        for r in cur.fetchall():
            claims.setdefault(r["id"], _compact_claim(r, as_of))
        if len(claims) < 10 and region_cbsa and region_cbsa != t["geo_key"]:   # widen to the region when thin
            cur.execute(
                _CLAIM_SELECT + """
                WHERE c.observed_at <= %s AND c.status NOT IN ('retracted','superseded') AND c.extractor_version LIKE 'extract@%%'
                  AND EXISTS (SELECT 1 FROM claim_geo g WHERE g.claim_id = c.id AND g.geo_key = %s)
                  AND c.attrs->>'domain' = %s ORDER BY c.observed_at DESC LIMIT %s""",
                (as_of, region_cbsa, t["domain"], MAX_CLAIMS - len(claims)),
            )
            for r in cur.fetchall():
                claims.setdefault(r["id"], _compact_claim(r, as_of))

        # --- delta since last review
        new_claims = [c for c in claims.values() if datetime.fromisoformat(c["observed"]).replace(tzinfo=UTC) > last_reviewed - timedelta(days=1)]
        cur.execute("""SELECT c.id, c.predicate, c.superseded_by FROM claims c WHERE c.id = ANY(%s) AND c.status = 'superseded'""",
                    (cited_ids or [0],))
        superseded = cur.fetchall()
        cur.execute("""SELECT k.id, k.claim_a, k.claim_b, k.kind, k.resolution, k.resolved_at FROM conflicts k
                       WHERE k.resolved_at BETWEEN %s AND %s AND (k.claim_a = ANY(%s) OR k.claim_b = ANY(%s) OR k.geo_key = %s)""",
                    (last_reviewed, as_of, list(claims) or [0], list(claims) or [0], t["geo_key"]))
        resolved_conflicts = cur.fetchall()
        geo_scope = [t["geo_key"]] + ([region_cbsa] if region_cbsa and region_cbsa != t["geo_key"] else [])
        cur.execute("""SELECT DISTINCT ON (a.indicator_id, a.geo_key) a.indicator_id, a.geo_key, a.period, a.value, a.zscore, a.yoy_pct, a.mom_pct
                       FROM indicator_anomalies a WHERE a.geo_key = ANY(%s) AND a.computed_at <= %s
                       ORDER BY a.indicator_id, a.geo_key, a.period DESC, a.computed_at DESC""", (geo_scope, as_of))
        anomalies = [dict(r) for r in cur.fetchall() if (r["zscore"] is not None and abs(float(r["zscore"])) >= 2) or
                     (r["yoy_pct"] is not None and abs(float(r["yoy_pct"])) >= 10)]
        falsifier_events = [{"falsifier_id": f["id"], "indicator_id": f["indicator_id"], "geo_key": f["geo_key"], "outcome": f["outcome"],
                             "last_check": f["last_check"]} for f in fals if f["last_check"] and f["last_check"].get("triggered")]

        # --- indicators allowlist: falsifier indicators + domain indicators at the thesis geo and region CBSA
        indicators: dict[str, dict] = {}
        wanted_keys = {(f["indicator_id"], f["geo_key"]) for f in fals}
        cur.execute("""SELECT DISTINCT p.indicator_id, p.geo_key FROM indicator_points p WHERE p.geo_key = ANY(%s)""", (geo_scope,))
        wanted_keys |= {(r["indicator_id"], r["geo_key"]) for r in cur.fetchall()}
        for ind, geo in sorted(wanted_keys):
            cur.execute("""SELECT DISTINCT ON (period) period, value FROM indicator_points
                           WHERE indicator_id = %s AND geo_key = %s AND observed_at <= %s ORDER BY period DESC, observed_at DESC LIMIT 4""",
                        (ind, geo, as_of))
            pts = cur.fetchall()
            if not pts:
                continue
            cur.execute("SELECT name, unit, direction_good FROM indicators WHERE id = %s", (ind,))
            meta = cur.fetchone() or {}
            indicators[f"{ind}@{geo}"] = {"name": meta.get("name"), "unit": meta.get("unit"),
                                          "points": [{"period": p["period"].isoformat(), "value": float(p["value"])} for p in pts]}

        # --- family theses (same region: same domain or same geo), as of
        cur.execute("""SELECT t2.id, t2.slug, t2.geo_key, t2.domain, v2.statement, v2.status, v2.confidence
                       FROM theses t2 JOIN LATERAL (SELECT * FROM thesis_versions x WHERE x.thesis_id = t2.id AND x.created_at <= %s
                                                    ORDER BY version DESC LIMIT 1) v2 ON true
                       WHERE t2.region_id = %s AND t2.id <> %s AND t2.active AND (t2.domain = %s OR t2.geo_key = %s)""",
                    (as_of, t["region_id"], thesis_id, t["domain"], t["geo_key"]))
        family = [dict(r) | {"confidence": float(r["confidence"])} for r in cur.fetchall()]
        leading = [{"cause": e["cause"], "effect": e["effect"], "lag_months": e["lag_months"], "status": e["status"],
                    "test_result": e["test_result"]} for e in edges if e["cause_indicator"]]

    current = {"id": thesis_id, "slug": t["slug"], "version": v["version"], "statement": v["statement"], "status": v["status"],
               "confidence": float(v["confidence"]), "confidence_reasoning": v["confidence_reasoning"], "implications": v["implications"],
               "change_summary": v["change_summary"], "known_disputes": v["known_disputes"],
               "support": [c["claim_id"] for c in cited if c["role"] == "support"],
               "counter": [c["claim_id"] for c in cited if c["role"] == "counter"],
               "causal_edges": [dict(e) for e in edges],
               "falsifiers": [{k: (v2.isoformat() if hasattr(v2, "isoformat") else v2) for k, v2 in dict(f).items()} for f in fals],
               "human_status": t["human_status"]}
    delta = {"new_claims": [c["id"] for c in new_claims], "superseded_cited_claims": [dict(r) for r in superseded],
             "resolved_conflicts": [dict(r) for r in resolved_conflicts], "indicator_moves": anomalies,
             "falsifiers_triggered": falsifier_events}
    notes = []
    if pre_existence_note:
        notes.append(pre_existence_note)
    if not claims:
        notes.append("No extracted claims are available for this thesis yet; only indicator points can be cited.")
    return Packet(thesis_id=thesis_id, region_id=t["region_id"], geo_key=t["geo_key"], domain=t["domain"], as_of=as_of, current=current,
                  delta=delta, family=family, leading=leading, claims=claims, indicators=indicators, last_reviewed_at=last_reviewed, notes=notes)


def delta_is_empty(p: Packet) -> bool:
    d = p.delta
    return not (d["new_claims"] or d["superseded_cited_claims"] or d["resolved_conflicts"] or d["indicator_moves"] or d["falsifiers_triggered"])

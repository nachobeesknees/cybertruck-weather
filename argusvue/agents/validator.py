"""Validator (code, no LLM except the entailment check): rejects proposals that cite outside the allowlist,
reference missing entities/indicators/geos, update without new support, exceed confidence rules, or restate
the thesis as a change summary. Implications are checked for entailment against their cited excerpts."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import date

import psycopg

from argusvue import llm
from argusvue.agents.packet import Packet
from argusvue.agents.schemas import AnalystProposal, EntailmentVerdict
from argusvue.quant.falsifiers import month_end
from argusvue.settings import PACKAGE_ROOT

ENTAILMENT_PROMPT = PACKAGE_ROOT / "agents" / "prompts" / "entailment.md"


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)      # hard: re-prompt once, then fail
    flags: list[str] = field(default_factory=list)       # soft: shown to the critic / stored
    entailment: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _indicator_key_ok(conn: psycopg.Connection, indicator_id: str, geo_key: str) -> tuple[bool, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM indicators WHERE id = %s", (indicator_id,))
        if cur.fetchone() is None:
            return False, f"indicator {indicator_id!r} does not exist"
        cur.execute("SELECT 1 FROM geo_units WHERE geo_key = %s", (geo_key,))
        if cur.fetchone() is None:
            return False, f"geo {geo_key!r} is not in the gazetteer"
        cur.execute("SELECT 1 FROM indicator_points WHERE indicator_id = %s AND geo_key = %s LIMIT 1", (indicator_id, geo_key))
        if cur.fetchone() is None:
            return False, f"no data for {indicator_id} at {geo_key}; a falsifier there can never be checked"
    return True, ""


def _latest_period(conn: psycopg.Connection, indicator_id: str, geo_key: str, as_of) -> date | None:
    with conn.cursor() as cur:
        cur.execute("SELECT max(period) AS p FROM indicator_points WHERE indicator_id = %s AND geo_key = %s AND observed_at <= %s",
                    (indicator_id, geo_key, as_of))
        row = cur.fetchone()
    return row["p"] if row else None


def validate(conn: psycopg.Connection, packet: Packet, proposal: AnalystProposal, *, check_entailment: bool = True,
             client=None, store=None) -> Validation:
    v = Validation()
    allow_c, allow_i = packet.allowlist_claims, packet.allowlist_indicators
    cited = set(proposal.support) | set(proposal.counter)
    for e in proposal.causal_edges:
        cited |= set(e.evidence)
    bad = sorted(c for c in cited if c not in allow_c)
    if bad:
        v.errors.append(f"claim ids outside the allowlist: {bad}")
    for i, imp in enumerate(proposal.implications):
        bad_i = [c for c in imp.cites if (isinstance(c, int) and c not in allow_c) or (isinstance(c, str) and c not in allow_i)]
        if bad_i:
            v.errors.append(f"implication {i} cites ids outside the allowlist: {bad_i}")
        if not imp.cites and not imp.hypothesis:
            v.errors.append(f"implication {i} cites nothing and is not tagged hypothesis")
    for i, f in enumerate(proposal.falsifiers):
        ok, why = _indicator_key_ok(conn, f.indicator_id, f.geo_key)
        if not ok:
            v.errors.append(f"falsifier {i}: {why}")
        try:
            if date.fromisoformat(f.by_date) <= packet.as_of.date():
                v.errors.append(f"falsifier {i}: by_date {f.by_date} is not in the future")
        except ValueError:
            v.errors.append(f"falsifier {i}: by_date {f.by_date!r} is not a date")
            continue
        if f.period and ok:
            target = date.fromisoformat(f.period + "-01")
            if target > date.fromisoformat(f.by_date):
                v.errors.append(f"falsifier {i}: period {f.period} is after by_date {f.by_date}")
            elif date.fromisoformat(f.by_date) < month_end(target, 1):
                v.errors.append(f"falsifier {i}: by_date {f.by_date} leaves no time for the {f.period} value to be published; "
                                f"use {month_end(target, 1).isoformat()} or later")
            latest = _latest_period(conn, f.indicator_id, f.geo_key, packet.as_of)
            if latest is not None and target <= latest:
                v.errors.append(f"falsifier {i}: period {f.period} is already observed (latest {latest.isoformat()}); "
                                "a same-period test must name a future period")
    for i, e in enumerate(proposal.causal_edges):
        if e.cause_indicator or e.effect_indicator:
            for ind in (e.cause_indicator, e.effect_indicator):
                if ind:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1 FROM indicators WHERE id = %s", (ind,))
                        if cur.fetchone() is None:
                            v.errors.append(f"causal edge {i}: indicator {ind!r} does not exist")
    prev_support = set(packet.current.get("support") or [])
    if proposal.action == "update":
        if not (set(proposal.support) - prev_support):
            v.errors.append("update proposes no support claim that the previous version did not already cite")
        prev_f = {(f["indicator_id"], f["geo_key"], f["operator"], float(f["threshold"])) for f in packet.current.get("falsifiers") or []}
        new_f = {(f.indicator_id, f.geo_key, f.operator, float(f.threshold)) for f in proposal.falsifiers}
        if not (new_f - prev_f):
            v.errors.append("update adds no new falsifier")
    if proposal.confidence > 0.6:
        origins = {packet.claims[c]["origin"] for c in proposal.support if c in packet.claims}
        if len(origins) < 2:
            v.errors.append(f"confidence {proposal.confidence} > 0.6 requires support from >= 2 distinct origins (have {sorted(origins)})")
    sim = difflib.SequenceMatcher(None, proposal.change_summary.lower(), proposal.statement.lower()).ratio()
    if proposal.action != "no_change" and (sim > 0.8 or len(proposal.change_summary.split()) < 6):
        v.errors.append("change_summary restates the thesis instead of saying what is new and why it matters")
    stale = [c for c in proposal.support if c in packet.claims and packet.claims[c]["past_half_life"]]
    if stale:
        v.flags.append(f"support claims past half-life: {stale}")
    low = [c for c in proposal.support if c in packet.claims and packet.claims[c]["effective_confidence"] < 0.3]
    if low:
        v.flags.append(f"support claims with effective confidence < 0.3: {low}")
    if proposal.status == "established" and proposal.confidence < 0.5:
        v.flags.append("status established with confidence below 0.5")
    if check_entailment and not v.errors:
        system = ENTAILMENT_PROMPT.read_text(encoding="utf-8")
        for i, imp in enumerate(proposal.implications):
            claim_cites = [c for c in imp.cites if isinstance(c, int) and c in packet.claims]
            if not claim_cites:
                continue
            excerpts = "\n".join(f"- [c:{c}] {packet.claims[c]['predicate']} = {packet.claims[c]['value']} {packet.claims[c]['unit'] or ''}: "
                                 f"\"{packet.claims[c]['excerpt']}\"" for c in claim_cites)
            user = f"Evidence:\n{excerpts}\n\nImplication: {imp.text}\nReturn JSON: {EntailmentVerdict.model_json_schema()}"
            try:
                res = llm.complete(conn, "entailment", system=system, user=user, schema=EntailmentVerdict, region_id=packet.region_id,
                                   geo_key=packet.geo_key, client=client, store=store, max_tokens=300)
            except llm.LLMError as e:
                v.flags.append(f"implication {i}: entailment check failed ({e})")
                continue
            v.entailment.append({"implication": i, "label": res.parsed.label, "reason": res.parsed.reason, "run_id": res.run_id})
            if res.parsed.label == "contradicted":
                v.errors.append(f"implication {i} is contradicted by its cited evidence: {res.parsed.reason}")
            elif res.parsed.label == "neutral":
                v.flags.append(f"implication {i} is not entailed by its cited evidence (neutral): {res.parsed.reason}")
    return v

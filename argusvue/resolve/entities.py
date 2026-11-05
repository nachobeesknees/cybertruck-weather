"""Entity resolution (SPEC §6): blocking on normalised name + type + region, scored candidates, LLM
adjudication only for ties; new entities are `candidate` until a second origin references them."""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from argusvue import llm
from argusvue.extract.schemas import EntityRef

log = logging.getLogger(__name__)

ACCEPT = 0.92
AMBIGUOUS = 0.78
_SUFFIXES = re.compile(r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|co|company|ltd|plc|lp|holdings|group|the)\b\.?", re.IGNORECASE)
_NONWORD = re.compile(r"[^a-z0-9 ]+")

RESOLVER_SYSTEM = """You decide whether a newly mentioned entity is the same real-world thing as one of the existing
candidate entities from a regional real-estate knowledge graph. Consider the name, type, place and identifiers. Answer
with the candidate id when it is the same entity, or null when it is a different one or you cannot tell."""


class ResolverVerdict(BaseModel):
    match_id: int | None
    reason: str = ""


@dataclass
class Resolved:
    entity_id: int
    created: bool
    method: str   # geo | identifier | exact | fuzzy | llm | new


def normalise(name: str) -> str:
    s = _SUFFIXES.sub(" ", name.lower())
    s = _NONWORD.sub(" ", s)
    return " ".join(s.split())


def _score(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def geo_entity(conn: psycopg.Connection, geo_key: str) -> int:
    """Canonical entity for a gazetteer unit (created on demand)."""
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM entity_ids WHERE scheme = 'geo_key' AND value = %s", (geo_key,))
        row = cur.fetchone()
        if row:
            return row["entity_id"]
        cur.execute("SELECT name, region_id FROM geo_units WHERE geo_key = %s", (geo_key,))
        g = cur.fetchone()
        if g is None:
            raise KeyError(f"{geo_key} not in gazetteer")
        cur.execute("""INSERT INTO entities(type, canonical_name, region_id, geo_key, status)
                       VALUES ('place', %s, %s, %s, 'canonical') RETURNING id""", (g["name"] or geo_key, g["region_id"], geo_key))
        eid = cur.fetchone()["id"]
        cur.execute("INSERT INTO entity_ids(entity_id, scheme, value) VALUES (%s, 'geo_key', %s)", (eid, geo_key))
    return eid


def resolve_entity(conn: psycopg.Connection, ref: EntityRef, *, region_id: str, geo_key: str | None,
                   origin_key: str, client=None, store=None) -> Resolved:
    # 1. identifiers (APN, case numbers, ...) are exact
    for scheme, value in (ref.identifiers or {}).items():
        with conn.cursor() as cur:
            cur.execute("SELECT entity_id FROM entity_ids WHERE scheme = %s AND value = %s", (scheme.lower(), value.strip()))
            row = cur.fetchone()
        if row:
            return Resolved(row["entity_id"], False, "identifier")
    # 2. places are their gazetteer unit
    if ref.type == "place" and geo_key and not geo_key.startswith("cbsa:") and geo_key != "country:US":
        return Resolved(geo_entity(conn, geo_key), False, "geo")
    norm = normalise(ref.name)
    if not norm:
        raise ValueError(f"entity name {ref.name!r} normalises to nothing")
    # 3. blocking: same type + region, alias match or shared first token
    first = norm.split()[0]
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.id, e.canonical_name, e.status, e.geo_key,
                      coalesce(array_agg(ei.value) FILTER (WHERE ei.scheme = 'alias'), '{}') AS aliases
               FROM entities e LEFT JOIN entity_ids ei ON ei.entity_id = e.id
               WHERE e.type = %s AND e.region_id IS NOT DISTINCT FROM %s AND e.merged_into IS NULL
                 AND (lower(e.canonical_name) LIKE %s OR EXISTS (
                        SELECT 1 FROM entity_ids x WHERE x.entity_id = e.id AND x.scheme = 'alias' AND x.value LIKE %s))
               GROUP BY e.id""",
            (ref.type, region_id, f"%{first}%", f"%{first}%"),
        )
        cands = cur.fetchall()
    scored: list[tuple[float, dict]] = []
    for c in cands:
        names = [c["canonical_name"], *c["aliases"]]
        best = max((_score(norm, normalise(n)) for n in names), default=0.0)
        if any(normalise(n) == norm for n in names):
            best = 1.0
        scored.append((best, c))
    scored.sort(key=lambda t: -t[0])
    if scored and scored[0][0] >= ACCEPT and (len(scored) == 1 or scored[1][0] < ACCEPT):
        c = scored[0][1]
        _add_alias(conn, c["id"], norm)
        return Resolved(c["id"], False, "exact" if scored[0][0] == 1.0 else "fuzzy")
    ambiguous = [c for s, c in scored if s >= AMBIGUOUS]
    if ambiguous:
        verdict = _adjudicate(conn, ref, geo_key, ambiguous, region_id, client=client, store=store)
        if verdict is not None:
            _add_alias(conn, verdict, norm)
            return Resolved(verdict, False, "llm")
    # 4. new candidate entity
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO entities(type, canonical_name, region_id, geo_key, status, attrs)
               VALUES (%s, %s, %s, %s, 'candidate', %s) RETURNING id""",
            (ref.type, ref.name.strip(), region_id, geo_key, Jsonb({"role": ref.role, "first_origin": origin_key,
                                                                    "place_hint": ref.place_hint})),
        )
        eid = cur.fetchone()["id"]
        cur.execute("INSERT INTO entity_ids(entity_id, scheme, value) VALUES (%s, 'alias', %s) ON CONFLICT DO NOTHING", (eid, norm))
        for scheme, value in (ref.identifiers or {}).items():
            cur.execute("INSERT INTO entity_ids(entity_id, scheme, value) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (eid, scheme.lower(), value.strip()))
    return Resolved(eid, True, "new")


def _add_alias(conn, entity_id: int, norm: str) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO entity_ids(entity_id, scheme, value) VALUES (%s, 'alias', %s) ON CONFLICT DO NOTHING",
                    (entity_id, norm))


def _adjudicate(conn, ref: EntityRef, geo_key, cands: list[dict], region_id: str, *, client=None, store=None) -> int | None:
    listing = "\n".join(f"- id={c['id']}: {c['canonical_name']} (status {c['status']}, geo {c['geo_key']}, aliases {list(c['aliases'])[:5]})"
                        for c in cands)
    user = (f"New mention: name={ref.name!r} type={ref.type} place_hint={ref.place_hint!r} address={ref.address!r} "
            f"role={ref.role!r} identifiers={ref.identifiers} resolved_geo={geo_key}\nCandidates:\n{listing}\n"
            f"Return JSON: {ResolverVerdict.model_json_schema()}")
    try:
        res = llm.complete(conn, "resolution", system=RESOLVER_SYSTEM, user=user, schema=ResolverVerdict,
                           region_id=region_id, geo_key=geo_key, client=client, store=store, max_tokens=400)
    except llm.LLMError as e:
        log.warning("resolver adjudication failed: %s", e)
        return None
    ids = {c["id"] for c in cands}
    return res.parsed.match_id if res.parsed.match_id in ids else None


def promote_candidates(conn: psycopg.Connection, region_id: str | None = None) -> int:
    """candidate -> canonical once claims from two distinct origins reference the entity."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE entities e SET status = 'canonical'
               WHERE e.status = 'candidate' AND (%s::text IS NULL OR e.region_id = %s)
                 AND (SELECT count(DISTINCT d.origin_key) FROM claims c JOIN documents d ON d.id = c.document_id
                      WHERE c.subject_entity = e.id OR c.object_entity = e.id) >= 2""",
            (region_id, region_id),
        )
        return cur.rowcount

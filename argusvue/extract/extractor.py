"""Document -> claims (SPEC §6). The model proposes claims with verbatim excerpts; code verifies every excerpt
against the document text, validates predicates and persons, resolves entities and geography, and writes
cited, append-only claims."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from argusvue import llm
from argusvue.extract.schemas import (
    DOMAINS,
    ExtractedClaim,
    Extraction,
    known_predicates,
    validate_claim,
    vocab_text,
)
from argusvue.ingest.base import IngestContext
from argusvue.reconcile.rules import half_life_for
from argusvue.resolve.entities import promote_candidates, resolve_entity
from argusvue.resolve.geocode import resolve_geo
from argusvue.store.blobs import BlobStore
from argusvue.store.documents import document_text
from argusvue.store.jobs import job_run

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "agents" / "prompts" / "extraction.md"
MIN_TEXT = 300
CHUNK = 28000
OVERLAP = 600
_WS = re.compile(r"\s+")


def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").replace("{{VOCAB}}", vocab_text())


def locate(text: str, excerpt: str) -> tuple[int, int] | None:
    """Exact span of `excerpt` in `text`, tolerating whitespace differences. Returns (start, end) in `text`."""
    ex = excerpt.strip()
    if not ex:
        return None
    i = text.find(ex)
    if i >= 0:
        return i, i + len(ex)
    # whitespace-normalised search with an index map back to the original text
    norm_chars: list[str] = []
    idx: list[int] = []
    prev_space = False
    for pos, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            prev_space = True
            norm_chars.append(" ")
            idx.append(pos)
        else:
            prev_space = False
            norm_chars.append(ch)
            idx.append(pos)
    norm = "".join(norm_chars)
    target = _WS.sub(" ", ex)
    j = norm.find(target)
    if j < 0:
        j = norm.lower().find(target.lower())
        if j < 0:
            return None
    return idx[j], idx[j + len(target) - 1] + 1


def chunks(text: str, size: int = CHUNK, overlap: int = OVERLAP) -> list[tuple[int, str]]:
    if len(text) <= size:
        return [(0, text)]
    out = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut
        out.append((start, text[start:end]))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


@dataclass
class ExtractStats:
    document_id: int
    status: str = "done"
    proposed: int = 0
    accepted: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    run_ids: list[int] = field(default_factory=list)
    cost_usd: float = 0.0
    claim_ids: list[int] = field(default_factory=list)

    def reject(self, why: str) -> None:
        self.rejected += 1
        self.reasons[why] = self.reasons.get(why, 0) + 1


def _doc(conn, document_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.*, s.slug AS source_slug, s.region_id, s.reliability, s.config AS source_config, s.kind AS source_kind
               FROM documents d LEFT JOIN sources s ON s.id = d.source_id WHERE d.id = %s""",
            (document_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"document {document_id} not found")
    return row


def _record(conn, document_id: int, phash: str, st: ExtractStats) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO document_extractions(document_id, prompt_hash, run_ids, status, claims, rejected, reasons)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (document_id, prompt_hash) DO UPDATE SET run_ids = EXCLUDED.run_ids, status = EXCLUDED.status,
                 claims = EXCLUDED.claims, rejected = EXCLUDED.rejected, reasons = EXCLUDED.reasons, created_at = now()""",
            (document_id, phash, st.run_ids, st.status, st.accepted, st.rejected, Jsonb(st.reasons)),
        )
    conn.commit()


def extract_document(conn: psycopg.Connection, document_id: int, *, store: BlobStore | None = None, client=None,
                     force: bool = False) -> ExtractStats:
    store = store or BlobStore()
    doc = _doc(conn, document_id)
    system = system_prompt()
    phash = llm.prompt_hash(system)
    st = ExtractStats(document_id=document_id)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM document_extractions WHERE document_id = %s AND prompt_hash = %s", (document_id, phash))
        prior = cur.fetchone()
    if prior and not force:
        st.status = "skipped"
        st.reasons["already_extracted"] = 1
        return st
    meta = doc["meta"] or {}
    if meta.get("near_dup_of"):
        st.status, st.reasons["near_duplicate"] = "skipped", 1
        _record(conn, document_id, phash, st)
        return st
    if (doc["source_kind"] or "") == "structured":
        st.status, st.reasons["structured_source"] = "skipped", 1
        _record(conn, document_id, phash, st)
        return st
    region_id = doc["region_id"]
    if not region_id:
        st.status, st.reasons["no_region"] = "skipped", 1
        _record(conn, document_id, phash, st)
        return st
    text = document_text(conn, store, document_id)
    if len(text) < MIN_TEXT:
        st.status, st.reasons["too_short"] = "skipped", 1
        _record(conn, document_id, phash, st)
        return st
    ctx = IngestContext(conn, IngestContext.load_region(conn, region_id), region_id, False, {})
    src_cfg = doc["source_config"] or {}
    tags = src_cfg.get("tags") or []
    header = (f"Document: {doc['title'] or '(untitled)'}\nURL: {doc['url']}\nPublished: {doc['published_at'] or 'unknown'}\n"
              f"Region: {region_id} (CBSA {ctx.cbsa_code}); counties {', '.join(ctx.county_fips)}\nSource tags: {', '.join(tags) or '-'}\n"
              f"Domains of interest: {', '.join(DOMAINS)}\n")
    accepted: list[tuple[ExtractedClaim, int, int, str, Extraction]] = []
    try:
        for offset, chunk in chunks(text):
            part = f"{header}\n--- DOCUMENT TEXT (characters {offset}-{offset + len(chunk)}) ---\n{chunk}\n--- END ---"
            res = llm.complete(conn, "extraction", system=system, user=part, schema=Extraction, region_id=region_id,
                               store=store, client=client, max_tokens=12000)
            st.run_ids.append(res.run_id)
            st.cost_usd += res.cost_usd
            st.proposed += len(res.parsed.claims)
            for c in res.parsed.claims:   # per-claim rejection (SPEC §6); the document is never re-asked for these
                problems = validate_claim(c)
                span = locate(chunk, c.excerpt)
                if span is None:
                    problems.append(f"excerpt not found verbatim in the document: {c.excerpt[:80]!r}")
                if problems:
                    for p in problems:
                        st.reject(p.split(":")[0][:60])
                    continue
                accepted.append((c, offset + span[0], offset + span[1], text[offset + span[0]:offset + span[1]], res.parsed))
        claim_ids = write_claims(conn, doc, ctx, accepted, phash, client=client, store=store, stats=st)
        st.claim_ids = claim_ids
        st.accepted = len(claim_ids)
        promote_candidates(conn, region_id)
        conn.commit()
    except llm.SpendCapExceeded:
        conn.rollback()
        st.status = "failed"
        st.reasons["spend_cap"] = 1
        _record(conn, document_id, phash, st)
        raise
    except llm.PendingAnswer:
        conn.rollback()            # session mode: the request is in the inbox; nothing recorded yet
        raise
    except Exception as e:
        conn.rollback()
        st.status = "failed"
        st.reasons[f"error:{type(e).__name__}"] = 1
        _record(conn, document_id, phash, st)
        raise
    _record(conn, document_id, phash, st)
    return st


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def claim_geo_key(ctx: IngestContext, c: ExtractedClaim, extraction: Extraction, src_cfg: dict) -> str | None:
    """Where a claim belongs. An explicit place hint that does not resolve inside the region means the fact
    is about somewhere else: benchmark sources map it to national scope, anything else is dropped (None).
    Without a hint, fall back to the document's main place, the monitor's geo scope, then the region CBSA."""
    from argusvue.resolve.geocode import geocode_address, geocode_hint

    if c.subject.address and (key := geocode_address(ctx, c.subject.address)):
        return key
    hint = c.subject.place_hint
    tags = src_cfg.get("tags") or []
    if hint:
        if (key := geocode_hint(ctx, hint)):
            return key
        if (extraction.primary_geo_hint and geocode_hint(ctx, extraction.primary_geo_hint)) and _mentions_region(ctx, hint):
            return geocode_hint(ctx, extraction.primary_geo_hint)
        return "country:US" if "benchmark" in tags else None
    for fb in (extraction.primary_geo_hint, src_cfg.get("geo_scope")):
        if fb and (key := (fb if fb in ctx.geo_keys else geocode_hint(ctx, fb))):
            return key
    if "benchmark" in tags:
        return "country:US"
    return f"cbsa:{ctx.cbsa_code}" if ctx.cbsa_code else "country:US"


def _mentions_region(ctx: IngestContext, hint: str) -> bool:
    h = hint.lower()
    names = [n.lower() for n in ctx.names.values() if n]
    return any(n.split(" county")[0] in h or n.split(" city")[0] in h for n in names if len(n) > 4)


def write_claims(conn, doc: dict, ctx: IngestContext, accepted, phash: str, *, client=None, store=None,
                 stats: ExtractStats | None = None) -> list[int]:
    ids: list[int] = []
    origin = doc["origin_key"] or doc["source_slug"]
    src_cfg = doc["source_config"] or {}
    for c, start, end, excerpt, extraction in accepted:
        geo_key = claim_geo_key(ctx, c, extraction, src_cfg)
        if geo_key is None:
            if stats is not None:
                stats.reject("geo_out_of_region")
            continue
        subj = resolve_entity(conn, c.subject, region_id=ctx.region_id, geo_key=geo_key, origin_key=origin, client=client, store=store)
        obj_id = None
        if c.object is not None:
            obj_geo = resolve_geo(ctx, place_hint=c.object.place_hint, address=c.object.address, fallbacks=[geo_key])
            obj_id = resolve_entity(conn, c.object, region_id=ctx.region_id, geo_key=obj_geo, origin_key=origin,
                                    client=client, store=store).entity_id
        kp = known_predicates()
        reliability = float(doc["reliability"] if doc["reliability"] is not None else 0.5)
        conf = round(reliability * float(c.confidence), 4)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO claims(subject_entity, predicate, object_entity, value_num, value_text, unit, geo_key, valid_from,
                                      valid_to, observed_at, document_id, excerpt_start, excerpt_end, excerpt, extractor_version,
                                      source_reliability, extraction_conf, confidence, status, half_life_days, attrs, extraction_run_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'asserted',%s,%s,%s) RETURNING id""",
                (subj.entity_id, c.predicate, obj_id, c.value_num, c.value_text, c.unit or (kp.get(c.predicate) or {}).get("unit"),
                 geo_key, _date(c.valid_from), _date(c.valid_to), doc["fetched_at"], doc["id"], start, end, excerpt,
                 f"extract@{phash}", reliability, float(c.confidence), conf, half_life_for(c.predicate),
                 Jsonb({"domain": c.domain, "vocab": "known" if c.predicate in kp else "extended",
                        "subject_name": c.subject.name, "subject_type": c.subject.type, "resolve_method": subj.method}),
                 None),
            )
            cid = cur.fetchone()["id"]
            cur.execute(
                """INSERT INTO claim_geo(claim_id, geo_key, level)
                   SELECT %s, g.geo_key, g.level FROM geo_units g WHERE g.geo_key = %s
                   UNION SELECT %s, a.ancestor_key, a.level FROM geo_ancestry a WHERE a.geo_key = %s
                   ON CONFLICT DO NOTHING""",
                (cid, geo_key, cid, geo_key),
            )
            cur.execute("INSERT INTO claim_origins(claim_id, origin_key) VALUES (%s, %s) ON CONFLICT DO NOTHING", (cid, origin))
        ids.append(cid)
    return ids


def run_extract(conn: psycopg.Connection, *, region_id: str | None = None, limit: int = 50, store: BlobStore | None = None,
                client=None, force: bool = False) -> dict:
    """Extract every web document (non-structured, non-duplicate) not yet processed with the current prompt."""
    system = system_prompt()
    phash = llm.prompt_hash(system)
    with job_run(conn, "extract.run", region_id) as run:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT d.id FROM documents d JOIN sources s ON s.id = d.source_id
                   WHERE s.kind <> 'structured' AND (%s::text IS NULL OR s.region_id = %s)
                     AND (d.meta->>'near_dup_of') IS NULL
                     AND (%s OR NOT EXISTS (SELECT 1 FROM document_extractions x WHERE x.document_id = d.id AND x.prompt_hash = %s))
                   ORDER BY d.fetched_at DESC LIMIT %s""",
                (region_id, region_id, force, phash, limit),
            )
            ids = [r["id"] for r in cur.fetchall()]
        totals = {"documents": 0, "done": 0, "skipped": 0, "failed": 0, "pending": 0, "proposed": 0, "accepted": 0,
                  "rejected": 0, "cost_usd": 0.0, "prompt_hash": phash}
        for did in ids:
            try:
                st = extract_document(conn, did, store=store, client=client, force=force)
            except llm.PendingAnswer:
                totals["pending"] += 1
                continue
            except llm.SpendCapExceeded:
                totals["failed"] += 1
                totals["stopped"] = "spend_cap"
                break
            except Exception:
                log.exception("extraction failed for document %s", did)
                totals["failed"] += 1
                continue
            totals["documents"] += 1
            totals[st.status] += 1
            totals["proposed"] += st.proposed
            totals["accepted"] += st.accepted
            totals["rejected"] += st.rejected
            totals["cost_usd"] = round(totals["cost_usd"] + st.cost_usd, 4)
        run.stats.update(totals)
        return dict(run.stats)

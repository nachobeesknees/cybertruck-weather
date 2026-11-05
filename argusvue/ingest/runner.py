"""Ingest runner: fetch -> snapshot -> dedup -> parse -> verify -> upsert points as claims.

Idempotency contract: an unchanged document is skipped entirely (document_ingests); a re-parsed
document inserts only points whose (indicator, geo, period) is new or whose value differs from the
current belief. A differing value is a *revision*: a new indicator_point row with a new observed_at,
a new claim, and the previous claim marked superseded (linked via superseded_by). Nothing is deleted.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from argusvue.ingest import registry
from argusvue.ingest.base import FetchedDocument, FetchSpec, IngestContext, Point, period_bounds
from argusvue.ingest.fetch import FetchResult, HttpFetcher
from argusvue.settings import get_settings
from argusvue.store.blobs import BlobStore
from argusvue.store.jobs import job_run

log = logging.getLogger(__name__)

EXTRACTOR_VERSION = "structured@1"
VERIFY_SAMPLE = 50
FULL_VERIFY_MAX_BYTES = 64 << 20


class IngestError(RuntimeError):
    pass


@dataclass
class DocStats:
    key: str
    url: str
    document_id: int
    new_document: bool
    skipped: bool = False
    parsed: int = 0
    dropped_out_of_region: int = 0
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0


def run_ingest(conn: psycopg.Connection, slug: str, region_id: str, *, backfill: bool = False,
               force: bool = False, source_cfg: registry.SourceConfig | None = None, fetcher=None,
               store: BlobStore | None = None) -> dict[str, Any]:
    """Run one source for one region ('*' = national context). Returns job stats."""
    cfg = source_cfg or registry.source(slug)
    if not cfg.applies_to(region_id) and region_id != "*":
        raise IngestError(f"source {cfg.slug} is not configured for region {region_id!r} (regions={cfg.regions})")
    if cfg.robots_disallowed and get_settings().strict_robots:
        raise IngestError(f"source {cfg.slug} is flagged robots_disallowed and STRICT_ROBOTS is on; skipping")
    registry.sync(conn, extra_sources=[cfg] if source_cfg is not None else None)
    conn.commit()
    store = store or BlobStore()
    fetcher = fetcher or HttpFetcher(store, delay_s=cfg.request_delay_s)
    region = IngestContext.load_region(conn, region_id) if region_id != "*" else None
    src = registry.source_class(cfg)()
    ctx = IngestContext(conn, region, region_id, backfill, cfg.config)
    with conn.cursor() as cur:
        cur.execute("SELECT id, reliability, origin_key FROM sources WHERE slug = %s", (cfg.slug,))
        src_row = cur.fetchone()
    if src_row is None:
        raise IngestError(f"source {cfg.slug} missing from sources table after sync")
    with job_run(conn, f"ingest.{cfg.slug}", region_id if region_id != "*" else None) as run:
        run.stats.update({"backfill": backfill, "force": force, "documents": []})
        specs = src.fetch_specs(ctx)
        totals = {"fetched": 0, "new_documents": 0, "skipped": 0, "parsed": 0, "dropped_out_of_region": 0,
                  "inserted": 0, "revised": 0, "unchanged": 0}
        for spec in specs:
            d = _process_spec(conn, ctx, src, cfg, src_row, spec, fetcher, store, force)
            run.stats["documents"].append(d.__dict__)
            totals["fetched"] += 1
            totals["new_documents"] += int(d.new_document)
            totals["skipped"] += int(d.skipped)
            for k in ("parsed", "dropped_out_of_region", "inserted", "revised", "unchanged"):
                totals[k] += getattr(d, k)
        run.stats.update(totals)
        return dict(run.stats)


def _process_spec(conn, ctx: IngestContext, src, cfg, src_row, spec: FetchSpec, fetcher, store, force) -> DocStats:
    res: FetchResult = fetcher.fetch(spec)
    fetched_at = datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents(source_id, url, fetched_at, content_hash, title, published_at, blob_ref, mime,
                                     size_bytes, origin_key, meta)
               VALUES (%s, %s, %s, decode(%s, 'hex'), %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (content_hash) DO UPDATE SET meta = documents.meta || EXCLUDED.meta
               RETURNING id, fetched_at, (xmax = 0) AS inserted""",
            (src_row["id"], res.final_url, fetched_at, res.blob.sha256, spec.title, spec.published_at, res.blob.ref,
             res.mime, res.blob.size, src_row["origin_key"],
             Jsonb({"key": spec.key, "requested_url": spec.url, "last_fetched_at": fetched_at.isoformat(), **spec.meta})),
        )
        row = cur.fetchone()
    conn.commit()
    doc = FetchedDocument(id=row["id"], url=res.final_url, key=spec.key, blob_path=store.path(res.blob.key),
                          fetched_at=row["fetched_at"], content_hash=res.blob.sha256, size=res.blob.size, mime=res.mime,
                          meta=spec.meta, is_new=row["inserted"])
    d = DocStats(key=spec.key, url=spec.url, document_id=doc.id, new_document=doc.is_new)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM document_ingests WHERE document_id=%s AND source_id=%s AND region_id=%s",
                    (doc.id, src_row["id"], ctx.region_id))
        done = cur.fetchone() is not None
    if done and not force:
        d.skipped = True
        log.info("skip %s: document %s already ingested for %s", spec.key, doc.id, ctx.region_id)
        return d

    points: list[Point] = []
    for p in src.parse(doc, ctx):
        d.parsed += 1
        if not ctx.in_region(p.geo_key):
            d.dropped_out_of_region += 1
            continue
        points.append(p)
    _verify_excerpts(doc, points)
    counts = upsert_points(conn, doc, src_row, points, cfg, ctx)
    d.inserted, d.revised, d.unchanged = counts["inserted"], counts["revised"], counts["unchanged"]
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO document_ingests(document_id, source_id, region_id, points, claims)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (document_id, source_id, region_id) DO UPDATE SET points=EXCLUDED.points,
                 claims=EXCLUDED.claims, finished_at=now()""",
            (doc.id, src_row["id"], ctx.region_id, len(points), counts["inserted"]),
        )
    conn.commit()
    log.info("%s %s: parsed=%d in_region=%d inserted=%d revised=%d unchanged=%d", cfg.slug, spec.key, d.parsed,
             len(points), d.inserted, d.revised, d.unchanged)
    return d


def _verify_excerpts(doc: FetchedDocument, points: list[Point]) -> None:
    """Reject any point whose excerpt is not found verbatim in the document text (SPEC §6)."""
    if not points:
        return
    full = doc.size <= FULL_VERIFY_MAX_BYTES
    sample = points if full else random.Random(doc.content_hash).sample(points, min(VERIFY_SAMPLE, len(points)))
    if full:
        text = doc.text()
    for p in sorted(sample, key=lambda p: (p.excerpt_start if p.excerpt_start is not None else -1)):
        if not p.excerpt:
            raise IngestError(f"{p.indicator_id} {p.geo_key} {p.period}: empty excerpt")
        if p.excerpt_start is not None and p.excerpt_end is not None:
            span = text[p.excerpt_start:p.excerpt_end] if full else doc.read_span(p.excerpt_start, p.excerpt_end)
            if span != p.excerpt:
                raise IngestError(
                    f"{p.indicator_id} {p.geo_key} {p.period}: excerpt offsets [{p.excerpt_start}:{p.excerpt_end}] "
                    f"do not match document text (got {span[:80]!r}, expected {p.excerpt[:80]!r})"
                )
        elif full:
            if p.excerpt not in text:
                raise IngestError(f"{p.indicator_id} {p.geo_key} {p.period}: excerpt not found verbatim in document")
        else:
            raise IngestError(f"{p.indicator_id} {p.geo_key} {p.period}: large document requires excerpt offsets")


def _num(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        return Decimal(repr(v))
    return Decimal(v)


def upsert_points(conn: psycopg.Connection, doc: FetchedDocument, src_row, points: list[Point], cfg, ctx) -> dict[str, int]:
    """Set-based upsert of points -> claims (+ claim_geo, claim_origins) -> indicator_points, with revisions."""
    if not points:
        return {"inserted": 0, "revised": 0, "unchanged": 0}
    indicators = registry.load_indicators()
    observed_at = doc.fetched_at
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """CREATE TEMP TABLE staged (indicator_id text, geo_key text, period date, valid_from date, valid_to date,
                   value numeric, unit text, excerpt text, excerpt_start int, excerpt_end int, attrs jsonb) ON COMMIT DROP"""
        )
        with cur.copy("COPY staged (indicator_id, geo_key, period, valid_from, valid_to, value, unit, excerpt, "
                      "excerpt_start, excerpt_end, attrs) FROM STDIN") as copy:
            for p in points:
                ind = indicators.get(p.indicator_id)
                if ind is None:
                    raise IngestError(f"unknown indicator id {p.indicator_id!r} (add it to config/indicators.yaml)")
                vf, vt = period_bounds(ind.frequency, p.period)
                attrs = {**p.attrs, "indicator_id": p.indicator_id, "period": p.period.isoformat()}
                copy.write_row((p.indicator_id, p.geo_key, p.period, p.valid_from or vf, p.valid_to or vt, _num(p.value),
                                p.unit, p.excerpt, p.excerpt_start, p.excerpt_end, json.dumps(attrs)))
        cur.execute("SELECT indicator_id, geo_key, period, count(*) AS n FROM staged GROUP BY 1,2,3 HAVING count(*) > 1 LIMIT 3")
        dups = cur.fetchall()
        if dups:
            raise IngestError(f"document yields duplicate (indicator, geo, period) rows, e.g. {dups}")
        cur.execute("SELECT DISTINCT s.geo_key FROM staged s LEFT JOIN geo_units g ON g.geo_key = s.geo_key WHERE g.geo_key IS NULL LIMIT 5")
        bad = [r["geo_key"] for r in cur.fetchall()]
        if bad:
            raise IngestError(f"points reference geo keys missing from the gazetteer: {bad}")
        cur.execute(
            """CREATE TEMP TABLE to_insert ON COMMIT DROP AS
               SELECT s.*, l.claim_id AS prev_claim_id
               FROM staged s LEFT JOIN indicator_latest l USING (indicator_id, geo_key, period)
               WHERE l.value IS NULL OR l.value <> s.value"""
        )
        cur.execute("SELECT count(*) AS n, count(prev_claim_id) AS revised FROM to_insert")
        r = cur.fetchone()
        n_new, n_rev = r["n"], r["revised"]
        cur.execute("SELECT count(*) AS n FROM staged")
        n_staged = cur.fetchone()["n"]
        if n_new == 0:
            return {"inserted": 0, "revised": 0, "unchanged": n_staged}

        # Geography entities (canonical, from the gazetteer) for every subject key.
        cur.execute(
            """WITH missing AS (
                   SELECT g.geo_key, g.name, g.region_id FROM geo_units g
                   WHERE g.geo_key IN (SELECT DISTINCT geo_key FROM to_insert)
                     AND NOT EXISTS (SELECT 1 FROM entity_ids ei WHERE ei.scheme = 'geo_key' AND ei.value = g.geo_key)
               ), ins AS (
                   INSERT INTO entities(type, canonical_name, region_id, geo_key, status)
                   SELECT 'place', COALESCE(name, geo_key), region_id, geo_key, 'canonical' FROM missing
                   RETURNING id, geo_key
               )
               INSERT INTO entity_ids(entity_id, scheme, value) SELECT id, 'geo_key', geo_key FROM ins"""
        )
        cur.execute(
            """INSERT INTO claims(subject_entity, predicate, value_num, unit, geo_key, valid_from, valid_to, observed_at,
                                  document_id, excerpt_start, excerpt_end, excerpt, extractor_version, source_reliability,
                                  extraction_conf, corroboration_count, confidence, status, half_life_days, attrs)
               SELECT ei.entity_id, 'indicator:' || t.indicator_id, t.value, COALESCE(t.unit, i.unit), t.geo_key,
                      t.valid_from, t.valid_to, %(observed_at)s, %(document_id)s, t.excerpt_start, t.excerpt_end, t.excerpt,
                      %(extractor_version)s, %(reliability)s, 1.0, 1, %(reliability)s, 'asserted', i.half_life_days, t.attrs
               FROM to_insert t
               JOIN indicators i ON i.id = t.indicator_id
               JOIN entity_ids ei ON ei.scheme = 'geo_key' AND ei.value = t.geo_key
               RETURNING id, attrs->>'indicator_id' AS indicator_id, geo_key, (attrs->>'period')::date AS period""",
            {"observed_at": observed_at, "document_id": doc.id, "extractor_version": f"{EXTRACTOR_VERSION}:{cfg.slug}",
             "reliability": src_row["reliability"]},
        )
        new_claims = cur.fetchall()
        cur.execute("CREATE TEMP TABLE new_claims (claim_id bigint, indicator_id text, geo_key text, period date) ON COMMIT DROP")
        with cur.copy("COPY new_claims (claim_id, indicator_id, geo_key, period) FROM STDIN") as copy:
            for c in new_claims:
                copy.write_row((c["id"], c["indicator_id"], c["geo_key"], c["period"]))
        cur.execute(
            """INSERT INTO indicator_points(indicator_id, geo_key, period, value, claim_id, observed_at)
               SELECT t.indicator_id, t.geo_key, t.period, t.value, n.claim_id, %s
               FROM to_insert t JOIN new_claims n USING (indicator_id, geo_key, period)""",
            (observed_at,),
        )
        if cur.rowcount != n_new:
            raise IngestError(f"internal: inserted {cur.rowcount} indicator points for {n_new} claims")
        cur.execute(
            """INSERT INTO claim_geo(claim_id, geo_key, level)
               SELECT n.claim_id, g.geo_key, g.level FROM new_claims n JOIN geo_units g ON g.geo_key = n.geo_key
               UNION
               SELECT n.claim_id, a.ancestor_key, a.level FROM new_claims n JOIN geo_ancestry a ON a.geo_key = n.geo_key
               ON CONFLICT DO NOTHING"""
        )
        cur.execute("INSERT INTO claim_origins(claim_id, origin_key) SELECT claim_id, %s FROM new_claims", (src_row["origin_key"],))
        cur.execute(
            """UPDATE claims c SET status = 'superseded', superseded_by = n.claim_id
               FROM to_insert t JOIN new_claims n USING (indicator_id, geo_key, period)
               WHERE t.prev_claim_id IS NOT NULL AND c.id = t.prev_claim_id"""
        )
        return {"inserted": n_new, "revised": n_rev, "unchanged": n_staged - n_new}


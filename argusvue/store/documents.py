"""Document storage shared by collectors: snapshot bytes, exact dedup by sha256, near-dedup by simhash,
origin inheritance for syndicated copies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from argusvue.collect.base import RawDocument
from argusvue.collect.dedup import hamming, simhash64, to_signed, to_unsigned
from argusvue.store.blobs import BlobStore

NEAR_DUP_MAX_DISTANCE = 3
NEAR_DUP_WINDOW_DAYS = 7          # syndication happens within days; templated monthlies are a month apart
MIN_TEXT_FOR_SIMHASH = 200


@dataclass
class StoredDocument:
    id: int
    status: str            # new | exact_dup | near_dup
    near_dup_of: int | None = None


def store_raw_document(conn: psycopg.Connection, store: BlobStore, raw: RawDocument, source_id: int,
                       region_id: str | None) -> StoredDocument:
    sha = hashlib.sha256(raw.content).hexdigest()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM documents WHERE content_hash = decode(%s, 'hex')", (sha,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE documents SET meta = meta || %s WHERE id = %s",
                        (Jsonb({"last_seen_at": raw.fetched_at.isoformat(), "last_seen_url": raw.url}), row["id"]))
            return StoredDocument(id=row["id"], status="exact_dup")
        suffix = ".pdf" if raw.content[:5] == b"%PDF-" else ".html" if "html" in (raw.mime or "") else ".bin"
        blob = store.put_bytes(raw.content, suffix=suffix)
        sh = simhash64(raw.text) if len(raw.text) >= MIN_TEXT_FOR_SIMHASH else None
        near_of = None
        origin = raw.origin_key
        if sh is not None:
            cur.execute(
                """SELECT id, simhash, origin_key FROM documents
                   WHERE simhash IS NOT NULL
                     AND (coalesce(published_at, fetched_at) BETWEEN %s - make_interval(days => %s) AND %s + make_interval(days => %s)
                          OR fetched_at > %s - make_interval(days => %s))
                     AND (%s::text IS NULL OR source_id IN (SELECT id FROM sources WHERE region_id = %s OR region_id IS NULL))
                   ORDER BY fetched_at DESC LIMIT 5000""",
                (raw.published_at or raw.fetched_at, NEAR_DUP_WINDOW_DAYS, raw.published_at or raw.fetched_at,
                 NEAR_DUP_WINDOW_DAYS, raw.fetched_at, NEAR_DUP_WINDOW_DAYS, region_id, region_id),
            )
            for cand in cur.fetchall():
                if hamming(sh, to_unsigned(cand["simhash"])) <= NEAR_DUP_MAX_DISTANCE:
                    near_of = cand["id"]
                    origin = cand["origin_key"] or origin
                    break
        meta = {**raw.meta, "discovered_via": raw.discovered_via}
        if near_of is not None:
            meta["near_dup_of"] = near_of
        cur.execute(
            """INSERT INTO documents(source_id, url, fetched_at, content_hash, simhash, title, published_at, blob_ref, mime,
                                     size_bytes, text_len, origin_key, meta)
               VALUES (%s, %s, %s, decode(%s, 'hex'), %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (source_id, raw.url, raw.fetched_at, sha, to_signed(sh) if sh is not None else None, raw.title, raw.published_at,
             blob.ref, raw.mime, len(raw.content), len(raw.text), origin, Jsonb(json.loads(json.dumps(meta, default=str)))),
        )
        doc_id = cur.fetchone()["id"]
        text_blob = store.put_bytes(raw.text.encode("utf-8"), suffix=".txt")
        cur.execute("UPDATE documents SET meta = meta || %s WHERE id = %s", (Jsonb({"text_ref": text_blob.ref}), doc_id))
    return StoredDocument(id=doc_id, status="near_dup" if near_of else "new", near_dup_of=near_of)


def document_text(conn: psycopg.Connection, store: BlobStore, document_id: int) -> str:
    """The extracted text a document's claims cite (text_ref blob), falling back to the raw blob."""
    with conn.cursor() as cur:
        cur.execute("SELECT blob_ref, meta FROM documents WHERE id = %s", (document_id,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"document {document_id} not found")
    ref = (row["meta"] or {}).get("text_ref")
    if ref:
        return store.get_bytes(ref).decode("utf-8")
    from datetime import UTC, datetime

    from argusvue.ingest.base import FetchedDocument  # structured documents: decode the raw file

    fd = FetchedDocument(id=document_id, url="", key="", blob_path=store.path(row["blob_ref"]), fetched_at=datetime.now(UTC),
                         content_hash="", size=0, mime=None, meta={}, is_new=False)
    return fd.text()

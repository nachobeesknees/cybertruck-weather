"""Precision audit (SPEC §10): sample claims into a CSV for human labelling; compute precision from labelled files."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import psycopg

from argusvue.settings import get_settings

FIELDS = ["claim_id", "predicate", "subject", "value", "unit", "geo_key", "valid_from", "excerpt", "document_url",
          "document_title", "source", "confidence", "label", "notes"]
LABELS = {"correct", "incorrect", "unsure"}


def sample(conn: psycopg.Connection, *, n: int = 100, since_days: int = 7, region_id: str | None = None,
           out_dir: Path | None = None, as_of: date | None = None) -> Path:
    out_dir = out_dir or (get_settings().out_dir / "audits")
    out_dir.mkdir(parents=True, exist_ok=True)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.predicate, e.canonical_name AS subject, coalesce(c.value_text, c.value_num::text) AS value, c.unit,
                      c.geo_key, c.valid_from, c.excerpt, d.url, d.title, s.slug AS source, c.confidence
               FROM claims c JOIN entities e ON e.id = c.subject_entity JOIN documents d ON d.id = c.document_id
               LEFT JOIN sources s ON s.id = d.source_id
               WHERE c.extractor_version LIKE 'extract@%%' AND c.created_at > now() - make_interval(days => %s)
                 AND (%s::text IS NULL OR e.region_id = %s)
               ORDER BY random() LIMIT %s""",
            (since_days, region_id, region_id, n),
        )
        rows = cur.fetchall()
    path = out_dir / f"{(as_of or date.today()).isoformat()}{'-' + region_id if region_id else ''}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({"claim_id": r["id"], "predicate": r["predicate"], "subject": r["subject"], "value": r["value"],
                        "unit": r["unit"] or "", "geo_key": r["geo_key"], "valid_from": r["valid_from"] or "",
                        "excerpt": r["excerpt"], "document_url": r["url"], "document_title": r["title"] or "",
                        "source": r["source"] or "", "confidence": r["confidence"], "label": "", "notes": ""})
    return path


def compute(audits_dir: Path | None = None) -> dict:
    """Precision from labelled audit files (label column: correct | incorrect | unsure)."""
    audits_dir = audits_dir or (get_settings().out_dir / "audits")
    labelled = 0
    correct = 0
    incorrect = 0
    unsure = 0
    by_pred: dict[str, list[int]] = {}
    by_source: dict[str, list[int]] = {}
    files = 0
    for p in sorted(audits_dir.glob("*.csv")):
        files += 1
        with p.open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lab = (row.get("label") or "").strip().lower()
                if lab not in LABELS:
                    continue
                labelled += 1
                if lab == "unsure":
                    unsure += 1
                    continue
                ok = lab == "correct"
                correct += ok
                incorrect += not ok
                cls = row["predicate"].split(".")[0]
                by_pred.setdefault(cls, [0, 0])[0 if ok else 1] += 1
                by_source.setdefault(row.get("source") or "?", [0, 0])[0 if ok else 1] += 1
    denom = correct + incorrect
    return {"files": files, "labelled": labelled, "correct": correct, "incorrect": incorrect, "unsure": unsure,
            "precision": round(correct / denom, 3) if denom else None,
            "by_predicate_class": {k: round(v[0] / (v[0] + v[1]), 3) for k, v in by_pred.items() if v[0] + v[1]},
            "by_source": {k: round(v[0] / (v[0] + v[1]), 3) for k, v in by_source.items() if v[0] + v[1]}}

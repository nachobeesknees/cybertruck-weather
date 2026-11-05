from __future__ import annotations

import csv
from datetime import UTC, datetime

import pytest

from argusvue.eval import precision

pytestmark = pytest.mark.db


def test_sample_and_compute(synthetic, tmp_path):
    conn = synthetic
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sources(slug, name, kind, region_id, reliability, origin_key) VALUES ('s','S','feed','tst',0.5,'o') RETURNING id")
        sid = cur.fetchone()["id"]
        cur.execute("INSERT INTO documents(source_id, url, fetched_at, content_hash, blob_ref, title) VALUES (%s,'https://x/1',%s,decode('aa','hex'),'b','T') RETURNING id",
                    (sid, datetime.now(UTC)))
        did = cur.fetchone()["id"]
        cur.execute("INSERT INTO entities(type, canonical_name, region_id, status) VALUES ('org','Acme','tst','candidate') RETURNING id")
        eid = cur.fetchone()["id"]
        for i in range(5):
            cur.execute("""INSERT INTO claims(subject_entity, predicate, value_num, geo_key, observed_at, document_id, excerpt,
                           extractor_version, source_reliability, extraction_conf, confidence)
                           VALUES (%s,'org.jobs',%s,'zcta:99001',now(),%s,'ex','extract@h',0.5,1,0.5)""", (eid, i, did))
    conn.commit()
    path = precision.sample(conn, n=3, out_dir=tmp_path, region_id="tst")
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 3 and rows[0]["label"] == "" and rows[0]["document_url"] == "https://x/1"
    rows[0]["label"], rows[1]["label"], rows[2]["label"] = "correct", "incorrect", "unsure"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=precision.FIELDS)
        w.writeheader()
        w.writerows(rows)
    rep = precision.compute(tmp_path)
    assert rep["labelled"] == 3 and rep["precision"] == 0.5 and rep["by_predicate_class"] == {"org": 0.5}

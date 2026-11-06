from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from argusvue.render.common import make_ctx
from argusvue.render.intel import EditorBrief, build_editor_packet, render_intel
from argusvue.store.blobs import BlobStore

pytestmark = pytest.mark.db


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.messages = self
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(parsed_output=EditorBrief.model_validate(self.payload), usage=SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0, server_tool_use=None),
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload), citations=None)], stop_reason="end_turn", stop_details=None)

    create = parse


def test_intel_report_cites_and_drops_untagged(synthetic, tmp_path):
    conn = synthetic
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sources(slug, name, kind, region_id, reliability, origin_key) VALUES ('s','S','structured','tst',0.9,'o'), ('n','N','feed','tst',0.7,'news.test') RETURNING id")
        sid, nid = [r["id"] for r in cur.fetchall()]
        cur.execute("INSERT INTO indicators(id, name, unit, direction_good, half_life_days, source_id, region_scope, frequency) VALUES ('zhvi','Home value','USD','neutral',45,%s,'{tst}','monthly')", (sid,))
        cur.execute("INSERT INTO documents(source_id, url, fetched_at, content_hash, blob_ref, title, origin_key) VALUES (%s,'u',now(),decode('aa','hex'),'b','Z','o'), (%s,'https://news.test/1',now(),decode('bb','hex'),'b','Council approves','news.test') RETURNING id", (sid, nid))
        d1, d2 = [r["id"] for r in cur.fetchall()]
        cur.execute("INSERT INTO entities(type, canonical_name, region_id, geo_key, status) VALUES ('place','Z','tst','cbsa:99999','canonical'), ('project','Deer Valley','tst','zcta:99001','candidate') RETURNING id")
        e1, e2 = [r["id"] for r in cur.fetchall()]
        for p, v in [("2026-08-01", 500000), ("2026-09-01", 510000)]:
            cur.execute("""INSERT INTO claims(subject_entity, predicate, value_num, geo_key, valid_from, observed_at, document_id, excerpt, extractor_version,
                           source_reliability, extraction_conf, confidence) VALUES (%s,'indicator:zhvi',%s,'cbsa:99999',%s,now(),%s,'r','structured@1',0.9,1,0.9) RETURNING id""", (e1, v, p, d1))
            cid = cur.fetchone()["id"]
            cur.execute("INSERT INTO indicator_points(indicator_id, geo_key, period, value, claim_id, observed_at) VALUES ('zhvi','cbsa:99999',%s,%s,%s,now())", (p, v, cid))
        cur.execute("""INSERT INTO claims(subject_entity, predicate, value_num, geo_key, valid_from, observed_at, document_id, excerpt, extractor_version,
                       source_reliability, extraction_conf, confidence, attrs) VALUES (%s,'project.units',320,'zcta:99001','2026-10-01',now(),%s,'approved a 320-unit complex','extract@h',0.7,0.9,0.63,'{"domain":"housing"}') RETURNING id""", (e2, d2))
        claim_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO claim_geo(claim_id, geo_key, level) SELECT %s, geo_key, level FROM geo_units WHERE geo_key='zcta:99001' UNION SELECT %s, ancestor_key, level FROM geo_ancestry WHERE geo_key='zcta:99001'", (claim_id, claim_id))
    conn.commit()
    ctx = make_ctx(conn, "tst", None, tmp_path)
    packet = build_editor_packet(ctx)
    assert claim_id in packet.allow_claims and "[i:zhvi:2026-09]" in packet.allow_indicators
    payload = {"headline": "Prices firm, one big project approved",
               "bottom_line": [f"A 320-unit complex was approved in 99001 [c:{claim_id}].", "Home values rose to 510,000 [i:zhvi:2026-09].",
                               "Analysts are certain prices will double next year."],
               "what_moved": [f"Deer Valley approval [c:{claim_id}]."], "signals": ["Values up month over month [i:zhvi:2026-09]."],
               "risks": ["Made-up risk with no evidence."], "watchlist": ["Watch the next ZHVI print [i:zhvi:2026-09]."],
               "notes_for_operator": "Evidence is thin."}
    r = render_intel(conn, "tst", None, tmp_path, client=FakeClient(payload), store=BlobStore(tmp_path / "b"))
    text = r.path.read_text()
    assert r.path.name.endswith("-tst.md") and "## Bottom line" in text and "## Key indicators" in text
    assert f"[c:{claim_id}]" in text and "[i:zhvi:2026-09]" in text
    assert "certain prices will double" not in text and "Made-up risk" not in text
    assert len(r.dropped) == 2 and "Evidence is thin." in text

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from argusvue.eval import calibration, conflicts, cost, dedup, recall, staleness
from argusvue.settings import get_settings

pytestmark = pytest.mark.db

NOW = datetime.now(UTC)


def _one(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()["id"]


@pytest.fixture
def world(synthetic):
    conn = synthetic
    with conn.cursor() as cur:
        cur.execute("TRUNCATE theses, agent_runs, document_extractions RESTART IDENTITY CASCADE")
        cur.execute("INSERT INTO indicators(id, name, unit, direction_good, half_life_days, region_scope, frequency) "
                    "VALUES ('ind.x','X','n','up',30,ARRAY['tst'],'monthly') ON CONFLICT DO NOTHING")
        feed = _one(cur, "INSERT INTO sources(slug,name,kind,region_id,reliability,origin_key) VALUES ('f','F','feed','tst',0.5,'f') RETURNING id")
        stru = _one(cur, "INSERT INTO sources(slug,name,kind,region_id,reliability,origin_key) VALUES ('s','S','structured','tst',0.9,'s') RETURNING id")

        def doc(src, n, title, published, fetched, meta=None):
            return _one(cur, """INSERT INTO documents(source_id,url,fetched_at,content_hash,blob_ref,title,published_at,meta)
                                VALUES (%s,%s,%s,decode(%s,'hex'),'b',%s,%s,%s) RETURNING id""",
                        (src, f"https://x/{n}", fetched, f"{n:02x}", title, published, Jsonb(meta or {})))

        pub = datetime(2025, 3, 4, tzinfo=UTC)
        d1 = doc(feed, 1, "TSMC Arizona fab expansion", pub, pub + timedelta(days=2))
        d2 = doc(feed, 2, "TSMC Arizona fab expansion (copy)", pub, pub + timedelta(days=3), {"near_dup_of": d1})
        d3 = doc(feed, 3, "Unrelated", NOW, NOW, {"last_seen_at": NOW.isoformat()})
        doc(stru, 4, "TSMC Arizona fab structured", pub, pub - timedelta(days=1))
        for d, h, st in ((d1, "h1", "done"), (d1, "h2", "done"), (d3, "h1", "done"), (d2, "h1", "skipped")):
            cur.execute("INSERT INTO document_extractions(document_id,prompt_hash,status) VALUES (%s,%s,%s)", (d, h, st))
        ent = _one(cur, "INSERT INTO entities(type,canonical_name,region_id,status) VALUES ('org','Acme','tst','candidate') RETURNING id")

        def claim(doc_id, age, hl, conf, ver="extract@h"):
            return _one(cur, """INSERT INTO claims(subject_entity,predicate,value_num,geo_key,observed_at,document_id,excerpt,
                                extractor_version,source_reliability,extraction_conf,confidence,half_life_days)
                                VALUES (%s,'org.jobs',1,'zcta:99001',%s,%s,'ex',%s,0.5,1,%s,%s) RETURNING id""",
                        (ent, NOW - timedelta(days=age), doc_id, ver, conf, hl))

        c1 = claim(d1, 100, 30, 0.8)   # eff 0.8*0.5^(100/30) ~ 0.08
        c2 = claim(d1, 10, 90, 0.9)    # eff ~ 0.83
        c3 = claim(d3, 1, 90, 0.9)
        claim(d3, 1, None, 0.9, ver="struct@1")
        # thesis approved v1 citing c1, c2
        t = _one(cur, "INSERT INTO theses(slug,region_id,geo_key,domain,human_status,approved_version,current_version) "
                      "VALUES ('t','tst','county:99001','housing','approved',1,1) RETURNING id")
        cur.execute("INSERT INTO thesis_versions(thesis_id,version,statement,status,confidence,change_summary) "
                    "VALUES (%s,1,'s','established',0.8,'seed')", (t,))
        for c in (c1, c2):
            cur.execute("INSERT INTO thesis_claims VALUES (%s,1,%s,'support')", (t, c))
        for act, outc in (("downgrade", "triggered"), ("review", "expired"), ("retire", None)):
            cur.execute("INSERT INTO falsifiers(thesis_id,version,indicator_id,geo_key,operator,threshold,by_date,action,outcome,resolved_at) "
                        "VALUES (%s,1,'ind.x','county:99001','<',1,current_date,%s,%s,%s)",
                        (t, act, outc, NOW if outc else None))
        # conflicts
        for (a, b), kind, opened, hours in (((c1, c2), "numeric_disagreement", NOW - timedelta(days=40), 10),
                                            ((c1, c3), "numeric_disagreement", NOW - timedelta(days=3), 20),
                                            ((c2, c3), "contradiction", NOW - timedelta(days=2), None)):
            cur.execute("INSERT INTO conflicts(claim_a,claim_b,kind,opened_at,resolved_at,region_id) VALUES (%s,%s,%s,%s,%s,'tst')",
                        (a, b, kind, opened, opened + timedelta(hours=hours) if hours else None))
        # spend
        for model, usd, ago in (("m1", 0.5, 0), ("m1", 1.0, 0), ("m2", 0.25, 3)):
            cur.execute("INSERT INTO agent_runs(role,model,region_id,input_tokens,output_tokens,cost_usd,ok,created_at) "
                        "VALUES ('extraction',%s,'tst',100,10,%s,true,%s)", (model, usd, NOW - timedelta(days=ago)))
        cur.execute("INSERT INTO job_runs(job,region_id,ok) VALUES ('ingest','tst',true),('ingest','tst',false)")
    conn.commit()
    return conn


def test_dedup(world):
    r = dedup.compute(world, "tst")["regions"]["tst"]
    assert r["raw_documents"] == 4 and r["near_duplicates"] == 1 and r["unique_documents"] == 3
    assert r["refetched_identical"] == 1
    assert r["extracted_documents"] == 2 and r["claims"] == 3 and r["claims_per_extracted_document"] == 1.5
    assert r["re_extracted_documents"] == 1 and r["re_extraction_rate"] == 0.5
    assert dedup.compute(world)["overall"]["raw_documents"] == 4


def test_staleness(world):
    r = staleness.compute(world, "tst")["approved_theses_claims"]
    assert r["claims"] == 2 and r["pct_past_half_life"] == 50.0 and r["pct_effective_below_0_4"] == 50.0
    assert 54.9 < r["median_age_days"] < 55.1
    with world.cursor() as cur:
        cur.execute("UPDATE theses SET human_status='pending'")
    fb = staleness.compute(world, "tst")
    assert fb["approved_theses_claims"]["claims"] == 0 and fb["all_extracted_claims"]["claims"] == 3 and "note" in fb


def test_conflicts(world):
    r = conflicts.compute(world, "tst")
    assert r["opened"] == {"all": 3, "7d": 2, "30d": 2} and r["resolved"] == {"all": 2, "7d": 1, "30d": 1}
    assert r["median_time_to_resolve_hours"] == 15.0 and r["p90_time_to_resolve_hours"] == 19.0
    assert r["open"] == 1 and r["open_by_kind"] == {"contradiction": 1} and 1.9 < r["oldest_open_age_days"] < 2.1
    assert conflicts.compute(world, "other")["opened"]["all"] == 0


def test_calibration(world):
    r = calibration.compute(world, "tst")
    assert r["by_action"]["downgrade"] == {"triggered": 1, "expired": 0, "open": 0}
    assert r["by_action"]["review"]["expired"] == 1 and r["by_action"]["retire"]["open"] == 1
    assert r["brier"] is None and r["n"] == 2
    assert calibration.compute(world, "tst", min_resolved=2)["brier"] == 0.34  # mean(0.8^2, 0.2^2)


def test_cost(world):
    r = cost.compute(world, "tst", days=7)
    assert r["totals"]["calls"] == 3 and r["totals"]["usd"] == 1.75 and r["totals"]["job_runs"] == 2
    assert r["totals"]["job_failures"] == 1
    assert len({x["day"] for x in r["llm"]}) == 2
    assert r["today"]["usd"] == 1.5 and r["today"]["cap_usd"] == get_settings().daily_spend_cap_usd
    assert cost.compute(world, "tst", days=1)["totals"]["calls"] == 2


def test_recall(world, tmp_path):
    p = tmp_path / "events.yaml"
    p.write_text("""events:
  - {id: hit, region: tst, date: 2025-03-04, title: T, keywords: [tsmc, fab, zzz]}
  - {id: miss, region: tst, date: 2025-03-04, title: M, keywords: [tsmc, volcano]}
  - {id: byurl, region: tst, date: 2025-03-04, title: U, keywords: [nothing], url: "https://x/1"}
  - {id: other, region: xxx, date: 2025-03-04, title: O, keywords: [tsmc, fab]}
""")
    r = recall.compute(world, "tst", events_path=p)
    st = {e["id"]: e for e in r["events"]}
    assert set(st) == {"hit", "miss", "byurl"}
    assert st["hit"]["captured"] and st["hit"]["latency_days"] == 2.0 and st["hit"]["matches"] == 2  # d1 + near-dup copy; structured excluded
    assert not st["miss"]["captured"] and st["byurl"]["captured"]
    assert r["regions"]["tst"]["pct_captured"] == 66.7 and r["regions"]["tst"]["median_latency_days"] == 2.0


def test_events_yaml_loads():
    evs = recall.load_events()
    assert {e["region"] for e in evs} == {"phx", "jh"} and all(e["url"] and len(e["keywords"]) >= 2 for e in evs)

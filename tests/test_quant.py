from __future__ import annotations

import random
import time
from datetime import date

import pytest

from argusvue.quant import check_falsifiers, compute_anomalies, latest_anomalies, run_quant, test_causal_edges

pytestmark = pytest.mark.db
GEO = "zcta:99001"


def _month(i: int, start=(2020, 1)) -> date:
    m = start[1] - 1 + i
    return date(start[0] + m // 12, m % 12 + 1, 1)


@pytest.fixture
def world(synthetic):
    conn = synthetic
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sources(slug, name, kind, region_id, reliability, origin_key) VALUES ('s','S','structured','tst',0.9,'o') RETURNING id")
        sid = cur.fetchone()["id"]
        cur.execute("INSERT INTO documents(source_id, url, fetched_at, content_hash, blob_ref) VALUES (%s,'u',now(),decode('aa','hex'),'b') RETURNING id", (sid,))
        did = cur.fetchone()["id"]
        cur.execute("INSERT INTO entities(type, canonical_name, region_id, geo_key, status) VALUES ('place','z','tst',%s,'canonical') RETURNING id", (GEO,))
        eid = cur.fetchone()["id"]
        for ind in ("a", "b", "c", "d", "e"):
            cur.execute("INSERT INTO indicators(id, name, unit, direction_good, half_life_days, source_id, region_scope, frequency) VALUES (%s,%s,'u','neutral',45,%s,'{tst}','monthly')", (ind, ind.upper(), sid))
    conn.commit()
    ctx = {"eid": eid, "did": did}

    def add(ind, values, start=(2020, 1), geo=GEO):
        with conn.cursor() as cur:
            for i, v in enumerate(values):
                p = _month(i, start)
                cur.execute("""INSERT INTO claims(subject_entity, predicate, value_num, geo_key, valid_from, observed_at, document_id, excerpt,
                               extractor_version, source_reliability, extraction_conf, confidence) VALUES (%s,%s,%s,%s,%s,now(),%s,'r','structured@1',0.9,1,0.9) RETURNING id""",
                            (eid, f"indicator:{ind}", v, geo, p, did))
                cid = cur.fetchone()["id"]
                cur.execute("INSERT INTO indicator_points(indicator_id, geo_key, period, value, claim_id, observed_at) VALUES (%s,%s,%s,%s,%s,now())", (ind, geo, p, v, cid))
        conn.commit()

    def thesis(slug="t"):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO theses(slug, region_id, geo_key, domain, current_version, approved_version) VALUES (%s,'tst',%s,'housing',1,1) RETURNING id", (slug, GEO))
            tid = cur.fetchone()["id"]
            cur.execute("INSERT INTO thesis_versions(thesis_id, version, statement, status, confidence, change_summary) VALUES (%s,1,'s','emerging',0.3,'c')", (tid,))
        conn.commit()
        return tid

    ctx.update(add=add, thesis=thesis)
    return conn, ctx


def test_anomalies_spike_and_short_series(world):
    conn, ctx = world
    vals = [100 + (i % 3) for i in range(39)] + [150]            # 40 months, spike at the end
    ctx["add"]("a", vals)
    ctx["add"]("b", [10, 11, 12, 13, 14, 15])                    # too short for a z-score
    stats = compute_anomalies(conn, "tst", last_n=3)
    assert stats["series"] == 2
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM indicator_anomalies WHERE indicator_id='a' ORDER BY period DESC")
        rows = cur.fetchall()
        assert len(rows) == 3 and rows[0]["period"] == _month(39)
        assert float(rows[0]["zscore"]) > 3
        assert float(rows[0]["mom_pct"]) == pytest.approx((150 - vals[38]) / vals[38] * 100)
        assert float(rows[0]["yoy_pct"]) == pytest.approx((150 - vals[27]) / vals[27] * 100)
        cur.execute("SELECT zscore, yoy_pct, mom_pct FROM indicator_anomalies WHERE indicator_id='b' ORDER BY period DESC LIMIT 1")
        b = cur.fetchone()
        assert b["zscore"] is None and b["yoy_pct"] is None and float(b["mom_pct"]) == pytest.approx(100 / 14)
    # rerun adds a new batch, never edits
    time.sleep(0.01)
    compute_anomalies(conn, "tst", last_n=3)
    with conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT computed_at) AS n, count(*) AS c FROM indicator_anomalies WHERE indicator_id='a'")
        r = cur.fetchone()
        assert r["n"] == 2 and r["c"] == 6
    top = latest_anomalies(conn, "tst")
    assert top[0]["indicator_id"] == "a" and top[0]["name"] == "A"


def test_falsifiers_trigger_hold_expire(world):
    conn, ctx = world
    ctx["add"]("a", [100.0] * 9, start=(2026, 1))
    ctx["add"]("b", [100.0] * 9, start=(2026, 1))
    ctx["add"]("c", [100.0] * 9, start=(2026, 1))
    ctx["add"]("a", [80.0], start=(2026, 10))                    # a crosses below 90 in 2026-10
    tid = ctx["thesis"]()
    rows = [("a", "<", 90, "2027-01-01"),                        # triggered
            ("b", "<", 90, "2027-12-31"),                        # open, not crossed, not yet due
            ("c", "<", 90, "2026-06-30")]                        # expired: value at by_date 100 >= 90
    with conn.cursor() as cur:
        for ind, op, thr, by in rows:
            cur.execute("INSERT INTO falsifiers(thesis_id, version, indicator_id, geo_key, operator, threshold, by_date, action) VALUES (%s,1,%s,%s,%s,%s,%s,'review')",
                        (tid, ind, GEO, op, thr, by))
    conn.commit()
    s = check_falsifiers(conn, "tst", today=date(2026, 11, 5))
    assert (s["checked"], s["triggered"], s["expired"]) == (3, 1, 1)
    with conn.cursor() as cur:
        cur.execute("SELECT indicator_id, outcome, resolved_at FROM falsifiers ORDER BY id")
        out = {r["indicator_id"]: r for r in cur.fetchall()}
        assert out["a"]["outcome"] == "triggered" and out["c"]["outcome"] == "expired"
        assert out["b"]["outcome"] is None and out["b"]["resolved_at"] is None
        cur.execute("SELECT kind, payload FROM thesis_events ORDER BY id")
        ev = cur.fetchall()
        assert sorted(e["kind"] for e in ev) == ["falsifier_expired", "falsifier_triggered"]
        trig = next(e for e in ev if e["kind"] == "falsifier_triggered")["payload"]
        assert trig["indicator_id"] == "a" and trig["value"] == 80 and trig["action"] == "review"
        cur.execute("SELECT count(*) AS n FROM falsifier_checks")
        assert cur.fetchone()["n"] == 3
    s2 = check_falsifiers(conn, "tst", today=date(2026, 11, 5))
    assert (s2["checked"], s2["triggered"], s2["expired"]) == (1, 0, 0)     # only b remains open
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM thesis_events")
        assert cur.fetchone()["n"] == 2


def test_leadlag_recovers_lag_and_flags_short_series(world):
    conn, ctx = world
    rng = random.Random(7)
    cause = [100.0]
    for _ in range(59):
        cause.append(cause[-1] * (1 + rng.gauss(0.005, 0.03)))
    effect = [None] * 60
    for t in range(60):
        base = cause[t - 3] if t >= 3 else cause[0]
        effect[t] = base * 2 * (1 + rng.gauss(0, 0.002))
    ctx["add"]("c", cause[:48])
    ctx["add"]("d", effect[:48])
    ctx["add"]("a", cause[:10])
    ctx["add"]("b", effect[:10])
    tid = ctx["thesis"]()
    with conn.cursor() as cur:
        for c, e in (("c", "d"), ("a", "b")):
            cur.execute("""INSERT INTO causal_edges(thesis_id, version, cause, effect, mechanism, lag_months, strength, cause_indicator, effect_indicator, geo_key)
                           VALUES (%s,1,'x','y','m',3,'weak',%s,%s,%s)""", (tid, c, e, GEO))
    conn.commit()
    stats = test_causal_edges(conn, "tst")
    assert stats["tested"] == 1 and stats["insufficient"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT cause_indicator, status, test_result FROM causal_edges ORDER BY id")
        good, short = cur.fetchall()
    assert good["status"] == "tested" and good["test_result"]["best_lag"] == 3
    assert good["test_result"]["method"] == "pearson_diff_log" and good["test_result"]["p_value_best"] < 0.001
    assert good["test_result"]["corr_at_hypothesised_lag"] == good["test_result"]["corr_best"]
    assert short["status"] == "hypothesis" and short["test_result"]["reason"] == "insufficient_overlap" and short["test_result"]["n"] == 10


def test_run_quant_records_job(world):
    conn, ctx = world
    ctx["add"]("a", [1.0, 2.0, 3.0])
    res = run_quant(conn, "tst")
    assert res["anomalies"]["series"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT ok FROM job_runs WHERE job='quant.run'")
        assert cur.fetchone()["ok"] is True


def test_falsifier_with_period_tests_only_that_period(world):
    from datetime import date as _d
    conn, ctx = world
    ctx["add"]("a", [100.0] * 10, start=(2026, 1))                  # 2026-01..2026-10, flat at 100
    ctx["add"]("a", [50.0], start=(2026, 11))                        # an off-season dip in 2026-11
    tid = ctx["thesis"]()
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO falsifiers(thesis_id, version, indicator_id, geo_key, operator, threshold, by_date, action, period)
                       VALUES (%s,1,'a',%s,'<',90,'2027-12-31','review','2027-10-01')""", (tid, GEO))
        cur.execute("""INSERT INTO falsifiers(thesis_id, version, indicator_id, geo_key, operator, threshold, by_date, action, period)
                       VALUES (%s,1,'a',%s,'<',90,'2026-12-31','review','2026-12-01')""", (tid, GEO))
    conn.commit()
    s = check_falsifiers(conn, "tst", today=_d(2026, 12, 5))
    assert s["triggered"] == 0 and s["no_data"] == 2                  # the 2026-11 dip is ignored: not the target period
    ctx["add"]("a", [80.0], start=(2027, 10))                        # the target month arrives, below the threshold
    s = check_falsifiers(conn, "tst", today=_d(2027, 11, 5))
    assert s["triggered"] == 1 and s["expired"] == 1                  # 2027-10 fires; 2026-12 never arrived and is past by_date
    with conn.cursor() as cur:
        cur.execute("SELECT period, outcome FROM falsifiers ORDER BY id")
        rows = cur.fetchall()
        assert rows[0]["outcome"] == "triggered" and rows[1]["outcome"] == "expired"
        cur.execute("SELECT payload FROM thesis_events WHERE kind = 'falsifier_triggered'")
        assert cur.fetchone()["payload"]["period"] == "2027-10-01"

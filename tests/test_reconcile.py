"""Reconcile rules (pure) and the engine on fixtures covering all four SPEC §6 cases + decay math."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from argusvue.reconcile import decay
from argusvue.reconcile.engine import reconcile_claim, run_reconcile
from argusvue.reconcile.rules import (
    combined_confidence,
    compare_values,
    decide,
    effective_confidence,
    predicate_class,
)


# ---- pure rules ----------------------------------------------------------------------------------
def test_predicate_classes_and_half_lives():
    assert predicate_class("listing.price").half_life_days == 14
    assert predicate_class("listing.status").half_life_days == 30
    assert predicate_class("project.status").half_life_days == 120
    assert predicate_class("org.jobs").half_life_days == 90
    assert predicate_class("zoning.decision").half_life_days == 730
    assert predicate_class("parcel.owner").half_life_days == 365
    assert predicate_class("something.else").name == "default"


def test_combined_confidence_diminishing_returns_and_cap():
    assert combined_confidence([0.7]) == 0.7
    assert combined_confidence([0.7, 0.6]) == pytest.approx(1 - 0.3 * 0.4)
    assert combined_confidence([0.9, 0.9, 0.9, 0.9]) == 0.95   # capped


def test_effective_confidence_halves_per_half_life():
    assert effective_confidence(0.8, 0, 30) == 0.8
    assert effective_confidence(0.8, 30, 30) == pytest.approx(0.4)
    assert effective_confidence(0.8, 60, 30) == pytest.approx(0.2)
    assert effective_confidence(0.8, 999, None) == 0.8


def test_compare_values_tolerance():
    assert compare_values("org.jobs", Decimal(2000), None, None, Decimal(2000), None, None) == "agree"
    assert compare_values("org.jobs", Decimal(2050), None, None, Decimal(2000), None, None) == "within_tolerance"   # 5%
    assert compare_values("org.jobs", Decimal(2500), None, None, Decimal(2000), None, None) == "contradict"
    assert compare_values("project.status", None, "Under construction", None, None, "under construction", None) == "agree"
    assert compare_values("project.status", None, "completed", None, None, "under construction", None) == "contradict"


def test_decide_table():
    kw = {"new_num": Decimal(10), "new_text": None, "new_obj": None, "old_num": Decimal(10), "old_text": None, "old_obj": None, "old_status": "asserted"}
    assert decide("project.units", new_valid_from=date(2026, 1, 1), new_valid_to=None, old_valid_from=date(2026, 1, 1),
                  old_valid_to=None, **kw).action == "corroborate"
    assert decide("project.units", new_valid_from=date(2026, 6, 1), new_valid_to=None, old_valid_from=date(2026, 1, 1),
                  old_valid_to=None, **kw).action == "supersede"
    assert decide("project.units", new_valid_from=date(2026, 6, 1), new_valid_to=date(2026, 6, 30),
                  old_valid_from=date(2026, 1, 1), old_valid_to=date(2026, 1, 31), **kw).action == "ignore"
    assert decide("project.units", new_valid_from=None, new_valid_to=None, old_valid_from=None, old_valid_to=None,
                  **{**kw, "old_status": "superseded"}).action == "ignore"


# ---- engine on the synthetic DB ------------------------------------------------------------------
pytestmark = pytest.mark.db


@pytest.fixture
def ledger(synthetic):
    """Two sources with distinct origins, two documents, one project entity."""
    with synthetic.cursor() as cur:
        cur.execute("""INSERT INTO sources(slug, name, kind, region_id, reliability, origin_key)
                       VALUES ('src-a','A','feed','tst',0.7,'news-a.test'), ('src-b','B','feed','tst',0.6,'news-b.test')
                       RETURNING id""")
        sa, sb = [r["id"] for r in cur.fetchall()]
        cur.execute("""INSERT INTO documents(source_id, url, fetched_at, content_hash, blob_ref, origin_key)
                       VALUES (%s,'https://news-a.test/1',now(),decode('aa','hex'),'local://a','news-a.test'),
                              (%s,'https://news-b.test/1',now(),decode('bb','hex'),'local://b','news-b.test')
                       RETURNING id""", (sa, sb))
        da, db_ = [r["id"] for r in cur.fetchall()]
        cur.execute("""INSERT INTO entities(type, canonical_name, region_id, geo_key, status)
                       VALUES ('project','Deer Valley Apartments','tst','zcta:99001','canonical') RETURNING id""")
        ent = cur.fetchone()["id"]
    synthetic.commit()
    return {"conn": synthetic, "docs": (da, db_), "entity": ent}


def _claim(conn, ent, doc, predicate, *, num=None, text=None, vf=None, vt=None, rel=0.7, observed=None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO claims(subject_entity, predicate, value_num, value_text, geo_key, valid_from, valid_to, observed_at,
                                  document_id, excerpt, extractor_version, source_reliability, extraction_conf, confidence,
                                  status, half_life_days)
               VALUES (%s,%s,%s,%s,'zcta:99001',%s,%s,%s,%s,'x','extract@1',%s,0.9,%s,'asserted',%s) RETURNING id""",
            (ent, predicate, num, text, vf, vt, observed or datetime.now(UTC), doc, rel, rel,
             predicate_class(predicate).half_life_days),
        )
        cid = cur.fetchone()["id"]
    conn.commit()
    return cid


def _get(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM claims WHERE id = %s", (cid,))
        return cur.fetchone()


def test_corroborate_new_origin_raises_confidence_once(ledger):
    conn, (da, db_), ent = ledger["conn"], ledger["docs"], ledger["entity"]
    a = _claim(conn, ent, da, "project.units", num=320, vf=date(2026, 1, 1), rel=0.7)
    assert reconcile_claim(conn, a) == "untouched"
    b = _claim(conn, ent, db_, "project.units", num=320, vf=date(2026, 1, 1), rel=0.6)
    assert reconcile_claim(conn, b) == "corroborate"
    ra, rb = _get(conn, a), _get(conn, b)
    assert ra["status"] == rb["status"] == "corroborated"
    assert ra["corroboration_count"] == 2
    assert float(ra["confidence"]) == pytest.approx(1 - 0.3 * 0.4)
    # same origin again (another doc from source A) does not add a count
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO documents(source_id, url, fetched_at, content_hash, blob_ref, origin_key)
                       SELECT source_id, 'https://news-a.test/2', now(), decode('ac','hex'), 'local://a2', origin_key
                       FROM documents WHERE id = %s RETURNING id""", (da,))
        da2 = cur.fetchone()["id"]
    conn.commit()
    c = _claim(conn, ent, da2, "project.units", num=320, vf=date(2026, 1, 1), rel=0.7)
    assert reconcile_claim(conn, c) == "corroborate"
    assert _get(conn, a)["corroboration_count"] == 2


def test_merge_within_tolerance_keeps_both_excerpts(ledger):
    conn, (da, db_), ent = ledger["conn"], ledger["docs"], ledger["entity"]
    a = _claim(conn, ent, da, "org.jobs", num=2000, vf=date(2026, 1, 1))
    b = _claim(conn, ent, db_, "org.jobs", num=2040, vf=date(2026, 1, 1))
    reconcile_claim(conn, a)
    assert reconcile_claim(conn, b) == "merge"
    ra, rb = _get(conn, a), _get(conn, b)
    assert ra["value_num"] == 2000 and rb["value_num"] == 2040        # both values survive
    assert ra["status"] == "corroborated" and ra["attrs"]["reconcile"] == "merge"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM conflicts")
        assert cur.fetchone()["n"] == 0


def test_contradiction_contests_both_and_opens_conflict(ledger):
    conn, (da, db_), ent = ledger["conn"], ledger["docs"], ledger["entity"]
    a = _claim(conn, ent, da, "project.status", text="under construction", vf=date(2026, 3, 1))
    b = _claim(conn, ent, db_, "project.status", text="cancelled", vf=date(2026, 3, 1))
    reconcile_claim(conn, a)
    assert reconcile_claim(conn, b) == "contest"
    assert _get(conn, a)["status"] == _get(conn, b)["status"] == "contested"
    with conn.cursor() as cur:
        cur.execute("SELECT claim_a, claim_b, kind, resolved_at, region_id FROM conflicts")
        c = cur.fetchone()
        assert (c["claim_a"], c["claim_b"], c["kind"], c["resolved_at"], c["region_id"]) == (a, b, "contradiction", None, "tst")


def test_newer_valid_from_supersedes_open_claim(ledger):
    conn, (da, db_), ent = ledger["conn"], ledger["docs"], ledger["entity"]
    a = _claim(conn, ent, da, "project.status", text="planned", vf=date(2025, 6, 1))
    b = _claim(conn, ent, db_, "project.status", text="under construction", vf=date(2026, 3, 1))
    reconcile_claim(conn, a)
    assert reconcile_claim(conn, b) == "supersede"
    ra = _get(conn, a)
    assert ra["status"] == "superseded" and ra["superseded_by"] == b and ra["valid_to"] == date(2026, 2, 28)
    assert _get(conn, b)["status"] == "asserted"


def test_run_reconcile_skips_structured_and_logs_job(ledger):
    conn, (da, db_), ent = ledger["conn"], ledger["docs"], ledger["entity"]
    _claim(conn, ent, da, "project.units", num=1, vf=date(2026, 1, 1))
    _claim(conn, ent, db_, "project.units", num=1, vf=date(2026, 1, 1))
    with conn.cursor() as cur:  # a structured claim must be left alone
        cur.execute("""INSERT INTO claims(subject_entity, predicate, value_num, geo_key, observed_at, document_id, excerpt,
                       extractor_version, source_reliability, extraction_conf, confidence, status)
                       VALUES (%s,'indicator:zhvi',5,'zcta:99001',now(),%s,'row','structured@1:zillow',0.9,1,0.9,'asserted')""",
                    (ent, da))
    conn.commit()
    st = run_reconcile(conn, region_id="tst")
    assert st["processed"] == 2 and st["corroborated"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM claims WHERE reconciled_at IS NULL")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT ok FROM job_runs WHERE job = 'reconcile.run' ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()["ok"]
    assert run_reconcile(conn, region_id="tst")["processed"] == 0   # idempotent


def test_stale_claims_use_sql_decay(ledger):
    conn, (da, _db), ent = ledger["conn"], ledger["docs"], ledger["entity"]
    old = _claim(conn, ent, da, "listing.price", num=500000, observed=datetime.now(UTC) - timedelta(days=30))  # hl 14d
    fresh = _claim(conn, ent, da, "zoning.decision", text="approved")                                          # hl 730d
    rows = decay.stale_claims(conn, load_bearing_only=False)
    ids = [r["id"] for r in rows]
    assert old in ids and fresh not in ids
    r = next(r for r in rows if r["id"] == old)
    assert float(r["effective"]) == pytest.approx(0.7 * 0.5 ** (30 / 14), rel=0.02)
    assert decay.claim_effective_confidence(_get(conn, old)) == pytest.approx(float(r["effective"]), rel=0.02)


def test_text_rules_same_document_fuzzy_and_free_text():
    kw = {"new_valid_from": None, "new_valid_to": None, "old_valid_from": None, "old_valid_to": None, "new_num": None, "old_num": None,
              "new_obj": None, "old_obj": None, "old_status": "asserted"}
    # same document never contests
    assert decide("housing.rules_amendment", new_text="Proposed: tighter rules", old_text="Proposed: income ranges", new_doc=5, old_doc=5, **kw).action == "ignore"
    # near-identical wording across outlets corroborates
    assert decide("policy.decision", new_text="EPA proposed approval", old_text="proposed approval by EPA", new_doc=1, old_doc=2, **kw).action == "corroborate"
    # long free text under one predicate is distinct, not contradictory
    assert decide("datacenter.status", new_text="roughly three dozen proposed facilities and half a dozen under construction",
                  old_text="160 data centers in Arizona, 149 in Phoenix", new_doc=1, old_doc=2, **kw).action == "ignore"
    # short categorical disagreement still contests
    assert decide("project.status", new_text="cancelled", old_text="approved", new_doc=1, old_doc=2, **kw).action == "contest"


def test_earlier_period_new_claim_is_superseded_not_contested(ledger):
    conn, (da, db_), ent = ledger["conn"], ledger["docs"], ledger["entity"]
    apr = _claim(conn, ent, da, "housing.months_supply", num=3.37, vf=date(2026, 4, 1))
    feb = _claim(conn, ent, db_, "housing.months_supply", num=4.42, vf=date(2026, 2, 1))
    reconcile_claim(conn, apr)
    assert reconcile_claim(conn, feb) == "supersede"
    rf, ra = _get(conn, feb), _get(conn, apr)
    assert rf["status"] == "superseded" and rf["superseded_by"] == apr and rf["valid_to"] == date(2026, 3, 31)
    assert ra["status"] == "asserted"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM conflicts")
        assert cur.fetchone()["n"] == 0

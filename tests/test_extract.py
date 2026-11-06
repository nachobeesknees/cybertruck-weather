"""Extractor contract with a fake model: verbatim excerpts enforced, predicates validated, entities resolved,
claims written with full citations; plus geocode and resolver unit behaviour on the synthetic gazetteer."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from argusvue.collect.base import RawDocument
from argusvue.extract.extractor import chunks, extract_document, locate
from argusvue.extract.schemas import EntityRef, ExtractedClaim, validate_claim
from argusvue.ingest.base import IngestContext
from argusvue.resolve.entities import normalise, promote_candidates, resolve_entity
from argusvue.resolve.geocode import geocode_hint
from argusvue.store.blobs import BlobStore
from argusvue.store.documents import store_raw_document

pytestmark = pytest.mark.db

TEXT = ("Council approves rezoning\n"
        "The Testville City Council on Wednesday approved a rezoning request for Deer Valley Apartments, a 320-unit "
        "apartment complex planned by Greystar Real Estate Partners near Deer Valley Road in ZIP 99001. The vote was 7-2.\n"
        "Councilmember Ana Lopez, who chairs the planning committee, said construction is expected to start in 2027.\n"
        "Separately, Chipmaker Corp said it will add 2,000 jobs at its Testville plant by 2028, a $1.2 billion expansion.\n"
        "A neighbour, Bob Smith, said he worried about traffic. " * 3)


def _claim(**kw):
    base = {"domain": "housing", "subject": {"name": "Deer Valley Apartments", "type": "project", "place_hint": "ZIP 99001"},
            "predicate": "project.units", "value_num": 320, "unit": "units", "valid_from": None, "valid_to": None,
            "excerpt": "a 320-unit apartment complex planned by Greystar Real Estate Partners", "confidence": 0.95}
    base.update(kw)
    return base


def _extraction(claims):
    return {"claims": claims, "document_summary": "Rezoning approved; chipmaker expansion.", "primary_geo_hint": "Testville",
            "published_date": None}


def _resp(payload: dict):
    from argusvue.extract.schemas import Extraction

    return SimpleNamespace(parsed_output=Extraction.model_validate(payload), usage=SimpleNamespace(
        input_tokens=5000, output_tokens=800, cache_read_input_tokens=0, cache_creation_input_tokens=0, server_tool_use=None),
        content=[SimpleNamespace(type="text", text=json.dumps(payload), citations=None)], stop_reason="end_turn", stop_details=None)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def parse(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)

    create = parse


@pytest.fixture
def web_doc(synthetic, tmp_path):
    with synthetic.cursor() as cur:
        cur.execute("""INSERT INTO sources(slug, name, kind, region_id, reliability, origin_key, config)
                       VALUES ('tst-news', 'News', 'feed', 'tst', 0.8, 'news.test', '{"tags": ["housing_market"]}') RETURNING id""")
        sid = cur.fetchone()["id"]
    synthetic.commit()
    store = BlobStore(tmp_path / "blobs")
    raw = RawDocument(url="https://news.test/story/1", fetched_at=datetime.now(UTC), content=TEXT.encode(), mime="text/html",
                      text=TEXT, title="Council approves rezoning", origin_key="news.test")
    stored = store_raw_document(synthetic, store, raw, sid, "tst")
    synthetic.commit()
    return synthetic, store, stored.id


def test_locate_tolerates_whitespace_and_returns_exact_span():
    text = "Alpha  beta\n gamma delta."
    assert locate(text, "beta gamma") == (7, 18) and text[7:18] == "beta\n gamma"
    assert locate(text, "beta gamma delta") == (7, 24)
    assert locate(text, "not here") is None


def test_chunks_cover_text_with_overlap():
    text = "x" * 70000
    cs = chunks(text, size=30000, overlap=500)
    assert cs[0][0] == 0 and cs[-1][0] + len(cs[-1][1]) == 70000 and len(cs) >= 3
    for (o1, c1), (o2, _c2) in itertools.pairwise(cs):
        assert o2 < o1 + len(c1)   # overlap


def test_validate_claim_rules():
    ok = ExtractedClaim.model_validate(_claim())
    assert validate_claim(ok) == []
    bad = ExtractedClaim.model_validate(_claim(value_num=None))
    assert any("needs value_num" in e for e in validate_claim(bad))
    person = ExtractedClaim.model_validate(_claim(subject={"name": "Bob Smith", "type": "person"}, predicate="person.statement",
                                                  value_text="worried", value_num=None, unit=None))
    assert any("public role" in e for e in validate_claim(person))
    with pytest.raises(ValueError):
        ExtractedClaim.model_validate(_claim(predicate="Units Approved"))


def test_geocode_hint_on_synthetic_gazetteer(synthetic):
    ctx = IngestContext(synthetic, IngestContext.load_region(synthetic, "tst"), "tst", False, {})
    assert geocode_hint(ctx, "ZIP 99001") == "zcta:99001"
    assert geocode_hint(ctx, "Test County, TL") == "county:99001"
    assert geocode_hint(ctx, "City of Testville") == "place:9900001"
    assert geocode_hint(ctx, "Testville, TL") == "place:9900001"
    assert geocode_hint(ctx, "Nowhere") is None


def test_extract_document_end_to_end(web_doc):
    conn, store, did = web_doc
    claims = [
        _claim(),
        _claim(subject={"name": "Chipmaker Corp", "type": "org", "place_hint": "Testville"}, domain="employment",
               predicate="org.jobs_announced", value_num=2000, unit="jobs", valid_from="2028-01-01",
               excerpt="Chipmaker Corp said it will add 2,000 jobs at its Testville plant by 2028"),
        _claim(subject={"name": "Chipmaker Corp", "type": "org", "place_hint": "Testville"}, domain="employment",
               predicate="org.investment_usd", value_num=1.2e9, unit="USD",
               excerpt="a $1.2 billion expansion"),
        _claim(subject={"name": "Testville City Council", "type": "org", "place_hint": "Testville"}, domain="policy",
               predicate="policy.vote", value_text="7-2", value_num=None, unit=None,
               excerpt="The vote was 7-2"),
        _claim(excerpt="this sentence does not exist in the document at all", predicate="project.status", value_text="approved",
               value_num=None, unit=None),                       # rejected: not verbatim
        _claim(subject={"name": "Bob Smith", "type": "person"}, domain="policy", predicate="person.concern", value_text="traffic",
               value_num=None, unit=None, excerpt="Bob Smith, said he worried about traffic"),   # rejected: private person
    ]
    # two bad claims are rejected individually; the four valid ones are written from the single attempt
    client = FakeClient([_resp(_extraction(claims))])
    st = extract_document(conn, did, store=store, client=client)
    assert st.status == "done" and st.accepted == 4 and st.rejected == 2
    assert "excerpt not found verbatim in the document" in st.reasons and "person subjects must carry a public role (official or executive); private individuals are not recorded"[:60] in st.reasons
    assert len(client.calls) == 1
    with conn.cursor() as cur:
        cur.execute("""SELECT c.*, e.canonical_name, e.type, e.status AS estatus FROM claims c JOIN entities e ON e.id = c.subject_entity
                       WHERE c.document_id = %s ORDER BY c.id""", (did,))
        rows = cur.fetchall()
    assert [r["predicate"] for r in rows] == ["project.units", "org.jobs_announced", "org.investment_usd", "policy.vote"]
    units = rows[0]
    assert units["value_num"] == 320 and units["geo_key"] == "zcta:99001" and units["canonical_name"] == "Deer Valley Apartments"
    assert units["excerpt"] == TEXT[units["excerpt_start"]:units["excerpt_end"]]
    assert float(units["confidence"]) == pytest.approx(0.8 * 0.95) and units["half_life_days"] == 180
    assert units["extractor_version"].startswith("extract@") and units["attrs"]["vocab"] == "known"
    jobs = rows[1]
    assert jobs["geo_key"] == "place:9900001" and jobs["type"] == "org" and jobs["estatus"] == "candidate"
    assert rows[2]["subject_entity"] == jobs["subject_entity"]           # same org resolved twice
    with conn.cursor() as cur:
        cur.execute("SELECT geo_key FROM claim_geo WHERE claim_id = %s ORDER BY level", (units["id"],))
        assert [r["geo_key"] for r in cur.fetchall()] == ["country:US", "state:99", "cbsa:99999", "county:99001",
                                                           "place:9900001", "sub:test-sub", "zcta:99001"]
        cur.execute("SELECT status, claims, rejected FROM document_extractions WHERE document_id = %s", (did,))
        rec = cur.fetchone()
        assert rec["status"] == "done" and rec["claims"] == 4
    # idempotent: second call is skipped without calling the model
    st2 = extract_document(conn, did, store=store, client=FakeClient([]))
    assert st2.status == "skipped" and st2.reasons == {"already_extracted": 1}


def test_resolver_exact_fuzzy_tie_and_promotion(synthetic, tmp_path):
    conn = synthetic
    ref = EntityRef(name="Greystar Real Estate Partners, LLC", type="org", place_hint="Testville")
    a = resolve_entity(conn, ref, region_id="tst", geo_key="place:9900001", origin_key="news.test")
    assert a.created and a.method == "new"
    b = resolve_entity(conn, EntityRef(name="Greystar Real Estate Partners", type="org"), region_id="tst", geo_key=None, origin_key="x")
    assert (b.entity_id, b.created, b.method) == (a.entity_id, False, "exact")
    c = resolve_entity(conn, EntityRef(name="Greystar Real Estate Partnrs Inc", type="org"), region_id="tst", geo_key=None, origin_key="x")
    assert (c.entity_id, c.method) == (a.entity_id, "fuzzy")
    assert normalise("The Greystar Real Estate Partners, LLC") == "greystar real estate partners"
    # a different org with a shared first token is not merged
    d = resolve_entity(conn, EntityRef(name="Greystar Development Fund IX", type="org"), region_id="tst", geo_key=None, origin_key="x")
    assert d.entity_id != a.entity_id
    # tie: two close candidates -> LLM adjudication picks one
    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities(type, canonical_name, region_id, status) VALUES ('project','Deer Valley Apartments Phase I','tst','candidate'), ('project','Deer Valley Apartments Phase II','tst','candidate') RETURNING id")
        _p1, p2 = [r["id"] for r in cur.fetchall()]
    conn.commit()
    verdict = {"match_id": p2, "reason": "phase II"}
    fake = FakeClient([SimpleNamespace(parsed_output=None, usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                       cache_creation_input_tokens=0, server_tool_use=None),
                                       content=[SimpleNamespace(type="text", text=json.dumps(verdict), citations=None)],
                                       stop_reason="end_turn", stop_details=None)])
    e = resolve_entity(conn, EntityRef(name="Deer Valley Apartments Phase 2", type="project"), region_id="tst", geo_key=None,
                       origin_key="x", client=fake, store=BlobStore(tmp_path))
    assert (e.entity_id, e.method) == (p2, "llm") and len(fake.calls) == 1
    # promotion needs two distinct origins
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO documents(source_id, url, fetched_at, content_hash, blob_ref, origin_key)
                       VALUES (NULL,'u1',now(),decode('01','hex'),'b','o1'), (NULL,'u2',now(),decode('02','hex'),'b','o2') RETURNING id""")
        d1, d2 = [r["id"] for r in cur.fetchall()]
        for doc in (d1, d1):
            cur.execute("""INSERT INTO claims(subject_entity, predicate, geo_key, observed_at, document_id, excerpt, extractor_version,
                           source_reliability, extraction_conf, confidence) VALUES (%s,'org.x','zcta:99001',now(),%s,'e','extract@h',0.5,1,0.5)""",
                        (a.entity_id, doc))
    conn.commit()
    assert promote_candidates(conn, "tst") == 0
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO claims(subject_entity, predicate, geo_key, observed_at, document_id, excerpt, extractor_version,
                       source_reliability, extraction_conf, confidence) VALUES (%s,'org.x','zcta:99001',now(),%s,'e','extract@h',0.5,1,0.5)""",
                    (a.entity_id, d2))
    conn.commit()
    assert promote_candidates(conn, "tst") == 1


def test_claim_geo_policy_out_of_region(synthetic):
    from argusvue.extract.extractor import claim_geo_key
    from argusvue.extract.schemas import Extraction

    ctx = IngestContext(synthetic, IngestContext.load_region(synthetic, "tst"), "tst", False, {})
    ex = Extraction(claims=[], document_summary="", primary_geo_hint="Testville")
    local = ExtractedClaim.model_validate(_claim(subject={"name": "X", "type": "project", "place_hint": "Testville"}))
    assert claim_geo_key(ctx, local, ex, {"tags": ["housing_market"]}) == "place:9900001"
    away = ExtractedClaim.model_validate(_claim(subject={"name": "X", "type": "project", "place_hint": "Austin, TX"}))
    assert claim_geo_key(ctx, away, ex, {"tags": ["data_centers"]}) is None
    assert claim_geo_key(ctx, away, ex, {"tags": ["benchmark"]}) == "country:US"
    nohint = ExtractedClaim.model_validate(_claim(subject={"name": "X", "type": "org"}))
    assert claim_geo_key(ctx, nohint, ex, {"tags": ["policy"]}) == "place:9900001"       # document's main place
    assert claim_geo_key(ctx, nohint, Extraction(claims=[], document_summary=""), {"tags": ["benchmark"]}) == "country:US"
    assert claim_geo_key(ctx, nohint, Extraction(claims=[], document_summary=""), {"tags": ["policy"]}) == "cbsa:99999"


def test_geocode_prefers_named_state_then_central_state():
    from argusvue.resolve.geocode import geocode_hint

    class Ctx:
        def __init__(self):
            self.state_fips = ["56", "16"]                      # jh: Teton WY (central) before Teton ID
            self.geo_keys = {"county:56039", "county:16081", "place:5640120"}
            self.counties = {("56", "teton county"): "county:56039", ("16", "teton county"): "county:16081"}

        def zcta(self, c):
            return None

        def county_by_name(self, name, st):
            fips = {"WY": "56", "ID": "16"}.get(st)
            return self.counties.get((fips, name.lower()))

        def place_by_name(self, name, st):
            return "place:5640120" if (name.lower(), st) == ("jackson", "WY") else None

    ctx = Ctx()
    assert geocode_hint(ctx, "Teton County, WY") == "county:56039"
    assert geocode_hint(ctx, "Teton County, Idaho") == "county:16081"
    assert geocode_hint(ctx, "Teton County") == "county:56039"          # region default: central state
    assert geocode_hint(ctx, "Jackson, Wyoming") == "place:5640120"

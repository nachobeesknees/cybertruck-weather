from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from argusvue.agents import theses as T
from argusvue.render.brief import render_brief
from argusvue.render.places import render_places
from argusvue.render.tags import Tag, enforce, split_sentences, tags_in
from argusvue.settings import get_settings

# ---- tags (pure) -----------------------------------------------------------------------------------------


def test_tags_in_parses_all_forms():
    s = "Prices rose [c:12] and rents fell [i:zhvi:2026-09] [i:mortgage30:2026-09-24] per run [run:7] but [c:x] [i:bad:2026] are junk."
    assert tags_in(s) == [Tag("c", "12"), Tag("i", "zhvi", "2026-09"), Tag("i", "mortgage30", "2026-09-24"), Tag("run", "7")]
    assert str(Tag("i", "zhvi", "2026-09")) == "[i:zhvi:2026-09]" and str(Tag("c", "3")) == "[c:3]"


def test_split_sentences_keeps_trailing_tags_and_abbreviations():
    assert split_sentences("Prices rose 3.5% in Mesa. [c:1] Rents fell [c:2]. Done!") == ["Prices rose 3.5% in Mesa. [c:1]", "Rents fell [c:2].", "Done!"]
    assert split_sentences("See e.g. the U.S. data [c:1]. Next one.") == ["See e.g. the U.S. data [c:1].", "Next one."]


def _all_valid(_):
    return True


def test_enforce_drops_untagged_and_keeps_tagged():
    md = "# Title\n\nPrices rose [c:1]. Everyone agrees it is great.\n\n- Rents fell [i:zhvi:2026-09].\n- An unsupported bullet.\n"
    out, dropped = enforce(md, valid=_all_valid)
    assert dropped == ["Everyone agrees it is great.", "An unsupported bullet."]
    assert "Prices rose [c:1]." in out and "Everyone" not in out and "unsupported" not in out
    assert "- Rents fell [i:zhvi:2026-09]." in out and out.startswith("# Title")


def test_enforce_exempts_headings_tables_notes_code_labels_and_is_stable():
    md = "## Section\n\n| a | b |\n|---|---|\n| x | y |\n\n> A note without tags.\n\n```\nuntagged code.\n```\n\n- **Label:**\n- Fine [c:1].\n"
    out, dropped = enforce(md, valid=_all_valid)
    assert dropped == [] and out == md


def test_enforce_rejects_invalid_ids():
    md = "Good [c:1]. Bad [c:2]. Mixed [c:1] [c:2].\n"
    out, dropped = enforce(md, valid=lambda t: t.ident == "1")
    assert dropped == ["Bad [c:2].", "Mixed [c:1] [c:2]."]
    assert out.strip() == "Good [c:1]."


def test_enforce_drops_whole_line_when_nothing_survives():
    out, dropped = enforce("- nothing here\n- ok [c:1]\n", valid=_all_valid)
    assert out.strip() == "- ok [c:1]"
    assert dropped == ["nothing here"]


# ---- DB-backed -------------------------------------------------------------------------------------------

TZ = ZoneInfo(get_settings().timezone)


def _today():
    return datetime.now(TZ).date()


@pytest.fixture
def world(synthetic, monkeypatch):
    conn = synthetic
    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_runs")
        cur.execute("INSERT INTO sources(slug, name, kind, region_id, reliability, origin_key) VALUES ('s','S','structured','tst',0.9,'o') RETURNING id")
        sid = cur.fetchone()["id"]
        cur.execute("INSERT INTO indicators(id, name, unit, direction_good, half_life_days, source_id, region_scope, frequency) "
                    "VALUES ('zhvi','Home value','USD','neutral',45,%s,'{tst}','monthly')", (sid,))
        cur.execute("INSERT INTO documents(source_id, url, title, fetched_at, content_hash, blob_ref) VALUES (%s,'http://x/y','Council minutes. Item 4',now(),decode('aa','hex'),'b') RETURNING id", (sid,))
        did = cur.fetchone()["id"]
        cur.execute("INSERT INTO entities(type, canonical_name, region_id, geo_key, status) VALUES ('place','Testville','tst','cbsa:99999','canonical') RETURNING id")
        eid = cur.fetchone()["id"]

        def claim(pred, val, geo, ext="structured@1", observed="now()", valid_from="2026-01-01", text=None):
            cur.execute(f"""INSERT INTO claims(subject_entity, predicate, value_num, value_text, geo_key, valid_from, observed_at, document_id, excerpt,
                            extractor_version, source_reliability, extraction_conf, confidence)
                            VALUES (%s,%s,%s,%s,%s,%s,{observed},%s,'r',%s,0.9,1,0.9) RETURNING id""", (eid, pred, val, text, geo, valid_from, did, ext))
            return cur.fetchone()["id"]

        w = {}
        # 14 monthly points at the CBSA: 2025-09 .. 2026-10
        for k in range(14):
            y, m = divmod(8 + k, 12)
            period = f"{2025 + y}-{m + 1:02d}-01"
            cid = claim("indicator:zhvi", 500000 + 1000 * k, "cbsa:99999", valid_from=period)
            cur.execute("INSERT INTO indicator_points(indicator_id, geo_key, period, value, claim_id, observed_at) VALUES ('zhvi','cbsa:99999',%s,%s,%s,now())",
                        (period, 500000 + 1000 * k, cid))
        w["last_period"] = "2026-10"
        for k, per in enumerate(["2026-08-01", "2026-09-01"]):
            cid = claim("indicator:zhvi", 300000 + k, "sub:test-sub", valid_from=per)
            cur.execute("INSERT INTO indicator_points(indicator_id, geo_key, period, value, claim_id, observed_at) VALUES ('zhvi','sub:test-sub',%s,%s,%s,now())",
                        (per, 300000 + k, cid))
        # a revision observed tomorrow, in a period nobody else has
        tcid = claim("indicator:zhvi", 999999, "cbsa:99999", observed="now() + interval '1 day'", valid_from="2027-01-01")
        cur.execute("INSERT INTO indicator_points(indicator_id, geo_key, period, value, claim_id, observed_at) VALUES ('zhvi','cbsa:99999','2027-01-01',999999,%s,now() + interval '1 day')", (tcid,))
        # contested claims + conflict
        w["ca"] = claim("permits_2026", 120, "cbsa:99999", ext="extract@1")
        w["cb"] = claim("permits_2026", 80, "cbsa:99999", ext="extract@1")
        cur.execute("INSERT INTO claim_geo(claim_id, geo_key, level) VALUES (%s,'cbsa:99999','cbsa'),(%s,'cbsa:99999','cbsa')", (w["ca"], w["cb"]))
        cur.execute("INSERT INTO conflicts(claim_a, claim_b, kind, region_id, geo_key) VALUES (%s,%s,'numeric_disagreement','tst','cbsa:99999')", (w["ca"], w["cb"]))
        # agent run today + open question
        cur.execute("INSERT INTO agent_runs(role, model, ok, cost_usd) VALUES ('analyst','opus-test',true,0.5) RETURNING id")
        w["run"] = cur.fetchone()["id"]
        cur.execute("INSERT INTO open_questions(region_id, geo_key, question, raised_by_run) VALUES ('tst','cbsa:99999','Is the permit count 120 or 80?',%s)", (w["run"],))
        # seeded thesis (no claims) with a falsifier check row
        seeds = {"seed-thesis": {"region": "tst", "geo_key": "zcta:99001", "domain": "housing", "statement": "Prices rise.", "confidence": 0.35,
                                 "falsifiers": [{"indicator_id": "zhvi", "geo_key": "cbsa:99999", "operator": "<", "threshold_rel": 0.95, "by_months": 12, "action": "review"}]}}
        monkeypatch.setattr(T, "load_seeds", lambda *a, **k: seeds)
    conn.commit()
    T.seed(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM falsifiers LIMIT 1")
        fid = cur.fetchone()["id"]
        cur.execute("INSERT INTO falsifier_checks(falsifier_id, period, value, triggered) VALUES (%s,'2026-10-01',512000,true)", (fid,))
        # thesis with a cited claim and a two-sentence change summary (second sentence is unsupported)
        cur.execute("INSERT INTO theses(slug, region_id, geo_key, domain) VALUES ('claim-thesis','tst','cbsa:99999','housing') RETURNING id")
        tid = cur.fetchone()["id"]
        cur.execute("INSERT INTO theses(slug, region_id, geo_key, domain) VALUES ('bare-thesis','tst','cbsa:99999','housing') RETURNING id")
        bare = cur.fetchone()["id"]
    conn.commit()
    T.write_version(conn, tid, statement="Permits are rising.", status="established", confidence=0.7, confidence_reasoning={}, implications=[],
                    change_summary="New permits data lifted the count. Analysts are certain it will double.", support=[w["ca"]], counter=[],
                    edges=[], falsifiers=[])
    T.write_version(conn, bare, statement="Nothing is known.", status="emerging", confidence=0.3, confidence_reasoning={}, implications=[],
                    change_summary="Placeholder.", support=[], counter=[], edges=[], falsifiers=[])
    conn.commit()
    w["tid"] = tid
    return conn, w


@pytest.mark.db
def test_brief_sections_tags_and_drops(world, tmp_path, caplog):
    conn, w = world
    with caplog.at_level(logging.WARNING):
        res = render_brief(conn, "tst", out_dir=tmp_path)
    assert res.path == tmp_path / "briefs" / f"{_today().isoformat()}-tst.md" and res.path.exists()
    text = res.path.read_text()
    for h in ("# Test brief", "## What changed since yesterday", "## Theses", "## Indicators", "## Open conflicts", "## Open questions", "## Cost today"):
        assert h in text, h
    assert text.index("## What changed") < text.index("## Theses") < text.index("## Indicators") < text.index("## Open conflicts") < \
        text.index("## Open questions") < text.index("## Cost today")
    # indicators table with a tag (no as_of: every observation counts)
    row = next(line for line in text.splitlines() if line.startswith("| Home value"))
    assert "[i:zhvi:" in row and "`cbsa:99999`" in row
    # conflict bullet carries both claims and values
    cline = next(line for line in text.splitlines() if line.startswith("- #1 (numeric_disagreement)"))
    assert f"[c:{w['ca']}]" in cline and f"[c:{w['cb']}]" in cline and "120" in cline and "80" in cline
    # question tagged with the run
    assert f"[run:{w['run']}]" in text
    # cost
    assert "| analyst | opus-test | 1 | $0.5000 |" in text
    # thesis grouping: established / emerging, claim tags, seed falls back to falsifier point, bare is a note
    assert text.index("### Established") < text.index("### Emerging")
    assert f"[c:{w['ca']}]" in next(line for line in text.splitlines() if line.startswith("- **claim-thesis**"))
    seed_line = next(line for line in text.splitlines() if line.startswith("- **seed-thesis**"))
    assert "[i:zhvi:2026-10]" in seed_line
    assert any(line.startswith("> **bare-thesis**") and "(no cited evidence yet)" in line for line in text.splitlines())
    # the injected unsupported sentence is dropped and reported
    assert "Analysts are certain it will double." in res.dropped and "Analysts are certain" not in text
    assert res.stats["dropped"] == len(res.dropped) >= 1
    assert any("Analysts are certain" in r.message for r in caplog.records)
    # what changed: versions in the window (v1 of each thesis), falsifier triggered, conflict opened
    assert "### Thesis versions" in text and "Falsifier on **seed-thesis** triggered" in text and "Conflict #1" in text


@pytest.mark.db
def test_brief_is_bitemporal(world, tmp_path):
    conn, _w = world
    today = _today()
    r_today = render_brief(conn, "tst", as_of=today, out_dir=tmp_path)
    text = r_today.path.read_text()
    assert "2027-01" not in text and "999,999" not in text
    row = next(line for line in text.splitlines() if line.startswith("| Home value"))
    assert "2026-10" in row and "[i:zhvi:2026-10]" in row
    r_later = render_brief(conn, "tst", as_of=today + timedelta(days=2), out_dir=tmp_path)
    assert "[i:zhvi:2027-01]" in r_later.path.read_text()
    # before anything existed: no thesis versions, no points
    r_early = render_brief(conn, "tst", as_of=today - timedelta(days=3), out_dir=tmp_path)
    early = r_early.path.read_text()
    assert "claim-thesis" not in early and "| Home value" not in early and "No active theses" in early


@pytest.mark.db
def test_brief_notes_spend_cap(world, tmp_path):
    conn, _ = world
    with conn.cursor() as cur:
        cur.execute("INSERT INTO agent_runs(role, model, ok, error) VALUES ('triage','haiku-test',false,'daily spend cap reached: $20.01 of $20.00')")
    conn.commit()
    text = render_brief(conn, "tst", out_dir=tmp_path).path.read_text()
    assert "Daily spend cap reached; jobs stopped." in text


@pytest.mark.db
def test_places_pages(world, tmp_path):
    conn, w = world
    results = render_places(conn, "tst", out_dir=tmp_path)
    paths = {r.path.name for r in results}
    assert {"cbsa_99999.md", "sub_test-sub.md"} <= paths
    cbsa = (tmp_path / "places" / "cbsa_99999.md").read_text()
    sub = (tmp_path / "places" / "sub_test-sub.md").read_text()
    for page in (cbsa, sub):
        for h in ("## Current theses", "## Indicator snapshot", "## Recent events"):
            assert h in page
    # seed thesis sits on zcta:99001: a descendant of the cbsa and a member of the submarket
    assert "**seed-thesis**" in cbsa and "**seed-thesis**" in sub
    assert "**claim-thesis**" in cbsa and "**claim-thesis**" in sub            # cbsa is an ancestor of the submarket
    assert "[i:zhvi:2026-10]" in cbsa and "[i:zhvi:2026-09]" in sub
    # events: claims from extract@ documents with claim_geo at the unit
    assert "Testville: permits_2026 = 120" in cbsa and "Council minutes; Item 4" in cbsa and f"[c:{w['ca']}]" in cbsa
    assert "Recent events" in sub and "permits_2026" not in sub

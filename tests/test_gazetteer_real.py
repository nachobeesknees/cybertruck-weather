"""Acceptance checks against the real loaded gazetteer (SPEC §11 phase 0)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from argusvue.cli import app
from argusvue.config import load_regions, load_submarkets
from argusvue.geo.resolve import resolve

pytestmark = pytest.mark.gazetteer


def test_phx_zcta_85085(gazetteer_db):
    r = resolve(gazetteer_db, "zcta:85085")
    assert r.region_id == "phx"
    assert r.ancestor_at("state") == "state:04"
    assert r.ancestor_at("cbsa") == "cbsa:38060"
    assert r.ancestor_at("county") == "county:04013"
    assert r.ancestor_at("place") == "place:0455000"
    assert "sub:phx-north-tsmc" in r.submarkets
    assert r.parent_key == "county:04013"


def test_jh_zcta_83014(gazetteer_db):
    r = resolve(gazetteer_db, "zcta:83014")
    assert r.region_id == "jh"
    assert r.ancestor_at("state") == "state:56"
    assert r.ancestor_at("cbsa") == "cbsa:27220"
    assert r.ancestor_at("county") == "county:56039"
    assert "sub:jh-wilson" in r.submarkets
    assert r.parent_key == "county:56039"


def test_all_seed_submarket_members_exist(gazetteer_db):
    subs = load_submarkets().submarkets
    members = sorted({m for s in subs.values() for m in s.members})
    with gazetteer_db.cursor() as cur:
        cur.execute("SELECT geo_key FROM geo_units WHERE geo_key = ANY(%s)", (members,))
        found = {r["geo_key"] for r in cur.fetchall()}
        cur.execute("SELECT geo_key FROM submarkets")
        loaded_subs = {r["geo_key"] for r in cur.fetchall()}
    assert set(members) == found
    assert {s.geo_key for s in subs.values()} == loaded_subs


def test_region_counties_match_delineation_and_containment(gazetteer_db):
    regions = load_regions().regions
    for reg in regions.values():
        with gazetteer_db.cursor() as cur:
            cur.execute("SELECT county_fips FROM cbsa_counties WHERE cbsa_code = %s", (reg.cbsa,))
            assert {r["county_fips"] for r in cur.fetchall()} == set(reg.counties)
        for c in reg.counties:
            r = resolve(gazetteer_db, f"county:{c}")
            assert r.ancestor_at("cbsa") == f"cbsa:{reg.cbsa}", c
        for c in reg.commuter_shed_counties:
            r = resolve(gazetteer_db, f"county:{c}")
            assert r.ancestor_at("cbsa") != f"cbsa:{reg.cbsa}", c
            assert r.region_id == reg.id


def test_configured_places_loaded_with_region(gazetteer_db):
    for reg in load_regions().regions.values():
        for p in reg.places:
            r = resolve(gazetteer_db, f"place:{p}")
            assert r.region_id == reg.id, p
            assert r.ancestor_at("state") == f"state:{p[:2]}"


def test_no_orphans(gazetteer_db):
    with gazetteer_db.cursor() as cur:
        cur.execute("SELECT geo_key FROM geo_units WHERE level <> 'country' AND parent_key IS NULL AND region_id IS NOT NULL")
        assert cur.fetchall() == []


def test_cli_resolve_runs(gazetteer_db):
    res = CliRunner().invoke(app, ["geo", "resolve", "zcta:83014"])
    assert res.exit_code == 0, res.output
    assert "cbsa:27220" in res.output and "sub:jh-wilson" in res.output

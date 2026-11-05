"""Containment-derived ancestry on a tiny synthetic gazetteer of squares (lon/lat degrees).

   state:99        [0,10]x[0,10]
   cbsa:99999      [0,6]x[0,6]
   county:99001    [0,3]x[0,3]        in cbsa
   county:99002    [7,10]x[7,10]      NOT in any cbsa
   place:9900001   [1,2]x[1,2]
   sub:test-sub    = zcta:99001 + zcta:99002
   zcta:99001      [1.2,1.8]x[1.2,1.8]  inside the place
   zcta:99002      [2.2,2.8]x[2.2,2.8]  outside the place
   tract:99001000100 [1.3,1.4]x[1.3,1.4]
"""

from __future__ import annotations

import pytest

from argusvue.geo.load import derive_attributes
from argusvue.geo.resolve import resolve

pytestmark = pytest.mark.db


def test_zcta_ancestors_include_every_coarser_container(synthetic):
    r = resolve(synthetic, "zcta:99001")
    assert r.ancestor_keys == ["country:US", "state:99", "cbsa:99999", "county:99001", "place:9900001", "sub:test-sub"]
    assert r.parent_key == "county:99001"
    assert r.region_id == "tst"
    assert r.submarkets == ["sub:test-sub"]
    assert len(r.h3) == 3


def test_zcta_outside_place_has_no_place_ancestor(synthetic):
    r = resolve(synthetic, "zcta:99002")
    assert r.ancestor_at("place") is None
    assert r.ancestor_at("sub") == "sub:test-sub"
    assert r.ancestor_at("county") == "county:99001"


def test_tract_ancestors_and_structural_parent(synthetic):
    r = resolve(synthetic, "tract:99001000100")
    assert r.parent_key == "county:99001"
    assert r.ancestor_at("zcta") == "zcta:99001"
    assert r.ancestor_at("place") == "place:9900001"
    assert r.ancestor_at("sub") == "sub:test-sub"
    assert r.ancestor_at("cbsa") == "cbsa:99999"


def test_county_outside_cbsa_has_state_parent_and_no_cbsa(synthetic):
    r = resolve(synthetic, "county:99002")
    assert r.ancestor_keys == ["country:US", "state:99"]
    assert r.parent_key == "state:99"


def test_county_in_cbsa_uses_delineation_for_parent(synthetic):
    r = resolve(synthetic, "county:99001")
    assert r.parent_key == "cbsa:99999"
    assert r.ancestor_keys == ["country:US", "state:99", "cbsa:99999"]


def test_place_is_not_inside_sub_but_sub_membership_is_explicit(synthetic):
    # A place's ancestors never include a sub by containment (sub is finer in the hierarchy)...
    r = resolve(synthetic, "place:9900001")
    assert r.ancestor_at("sub") is None
    assert r.ancestor_keys == ["country:US", "state:99", "cbsa:99999", "county:99001"]


def test_h3_cell_resolves_by_point(synthetic):
    import h3

    cell = h3.latlng_to_cell(1.5, 1.5, 8)   # lat, lon inside zcta:99001
    r = resolve(synthetic, f"h3:{cell}")
    assert r.ancestor_at("zcta") == "zcta:99001"
    assert r.ancestor_at("county") == "county:99001"
    assert r.region_id == "tst"
    assert r.found


def test_unknown_but_well_formed_key_errors(synthetic):
    from argusvue.geo.keys import GeoKeyError

    with pytest.raises(GeoKeyError):
        resolve(synthetic, "zcta:00000")


def test_derive_attributes_is_idempotent(synthetic):
    with synthetic.cursor() as cur:
        cur.execute("SELECT geo_key, parent_key, region_id FROM geo_units ORDER BY geo_key")
        before = cur.fetchall()
    derive_attributes(synthetic)
    with synthetic.cursor() as cur:
        cur.execute("SELECT geo_key, parent_key, region_id FROM geo_units ORDER BY geo_key")
        after = cur.fetchall()
    assert before == after


def test_materialised_ancestry_matches_dynamic_function(synthetic):
    with synthetic.cursor() as cur:
        cur.execute("SELECT geo_key FROM geo_units WHERE level <> 'country' ORDER BY 1")
        keys = [r["geo_key"] for r in cur.fetchall()]
        for k in keys:
            cur.execute("SELECT ancestor_key FROM geo_ancestry WHERE geo_key = %s ORDER BY 1", (k,))
            mat = [r["ancestor_key"] for r in cur.fetchall()]
            cur.execute("""SELECT geo_key FROM geo_ancestors(%s)
                           UNION SELECT s.geo_key FROM submarkets s WHERE %s = ANY(s.member_keys) ORDER BY 1""", (k, k))
            dyn = [r["geo_key"] for r in cur.fetchall()]
            assert mat == dyn, k

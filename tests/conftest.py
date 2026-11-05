from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from argusvue.settings import get_settings
from argusvue.store.migrate import migrate


def _test_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or get_settings().test_database_url


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = _test_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        psycopg.connect(url).close()
    except psycopg.OperationalError as e:
        pytest.skip(f"test database unreachable: {e}")
    return url


@pytest.fixture(scope="session")
def migrated_test_db(test_db_url: str) -> str:
    with psycopg.connect(test_db_url, row_factory=dict_row) as conn:
        migrate(conn)
        conn.commit()
    return test_db_url


@pytest.fixture
def db(migrated_test_db: str):
    """A connection to the migrated test database. Each test gets a fresh one; nothing is cleaned
    automatically because the synthetic-gazetteer tests manage their own rows."""
    conn = psycopg.connect(migrated_test_db, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture(scope="session")
def gazetteer_db():
    """The real, loaded gazetteer (main DATABASE_URL). Skips unless both regions are loaded."""
    url = get_settings().database_url
    try:
        conn = psycopg.connect(url, row_factory=dict_row)
    except psycopg.OperationalError as e:
        pytest.skip(f"database unreachable: {e}")
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT count(*) AS n FROM regions WHERE id IN ('phx','jh')")
            n = cur.fetchone()["n"]
        except psycopg.Error:
            n = 0
    conn.rollback()
    if n < 2:
        conn.close()
        pytest.skip("gazetteer not loaded for both regions (run `argusvue geo load --region phx|jh`)")
    yield conn
    conn.close()


# ---- synthetic gazetteer (squares in lon/lat degrees) ---------------------------------------------
#   state:99 [0,10]^2 > cbsa:99999 [0,6]^2 > county:99001 [0,3]^2 (in cbsa) ; county:99002 [7,10]^2 (no cbsa)
#   place:9900001 [1,2]^2 ; zcta:99001 [1.2,1.8]^2 (in place) ; zcta:99002 [2.2,2.8]^2 ; tract:99001000100 [1.3,1.4]^2
#   sub:test-sub = zcta:99001 + zcta:99002


def _box(x0, y0, x1, y1) -> str:
    return f"MULTIPOLYGON((({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0})))"


SYNTHETIC_UNITS = [
    ("country:US", "country", "US", None),
    ("state:99", "state", "99", _box(0, 0, 10, 10)),
    ("cbsa:99999", "cbsa", "99999", _box(0, 0, 6, 6)),
    ("county:99001", "county", "99001", _box(0, 0, 3, 3)),
    ("county:99002", "county", "99002", _box(7, 7, 10, 10)),
    ("place:9900001", "place", "9900001", _box(1, 1, 2, 2)),
    ("zcta:99001", "zcta", "99001", _box(1.2, 1.2, 1.8, 1.8)),
    ("zcta:99002", "zcta", "99002", _box(2.2, 2.2, 2.8, 2.8)),
    ("tract:99001000100", "tract", "99001000100", _box(1.3, 1.3, 1.4, 1.4)),
]


@pytest.fixture
def synthetic(db):
    """Wipe the test DB's geography + ledger tables and load the synthetic gazetteer."""
    from argusvue.geo.load import derive_attributes, refresh_ancestry

    with db.cursor() as cur:
        cur.execute(
            """TRUNCATE indicator_points, claim_geo, claim_origins, entity_links, claims, entity_ids, entities,
                        document_ingests, documents, indicators, sources, geo_ancestry, submarkets, geo_units,
                        cbsa_counties, regions, job_runs RESTART IDENTITY CASCADE"""
        )
        cur.execute("INSERT INTO regions(id, name, cbsa_code, county_fips) VALUES ('tst','Test','99999',ARRAY['99001'])")
        cur.execute(
            "INSERT INTO cbsa_counties VALUES ('99001','99999','Test, XX','Metropolitan Statistical Area','Central','test')"
        )
        for key, level, code, wkt in SYNTHETIC_UNITS:
            cur.execute(
                """INSERT INTO geo_units(geo_key, level, code, name, region_id, geom, vintage)
                   VALUES (%s, %s, %s, %s, CASE WHEN %s IN ('county','place','zcta','tract') THEN 'tst' END,
                           ST_SetSRID(ST_GeomFromText(%s), 4326), 'test')""",
                (key, level, code, key, level, wkt),
            )
        derive_attributes(db)
        cur.execute(
            """INSERT INTO geo_units(geo_key, level, code, name, region_id, geom, vintage)
               SELECT 'sub:test-sub','sub','test-sub','Test Sub','tst', ST_Multi(ST_Union(geom)), 'config'
               FROM geo_units WHERE geo_key IN ('zcta:99001','zcta:99002')"""
        )
        cur.execute(
            """INSERT INTO submarkets(geo_key, region_id, name, member_keys, geom)
               SELECT geo_key, 'tst', name, ARRAY['zcta:99001','zcta:99002'], geom FROM geo_units WHERE geo_key='sub:test-sub'"""
        )
        derive_attributes(db)
        refresh_ancestry(db)
    db.commit()
    yield db

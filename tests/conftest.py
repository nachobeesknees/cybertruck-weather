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

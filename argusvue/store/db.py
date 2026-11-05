"""Thin psycopg v3 helpers. No ORM: parameterised SQL only."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from argusvue.settings import get_settings


def connect(url: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection returning dict rows. Caller closes it (or use `connection()`)."""
    return psycopg.connect(url or get_settings().database_url, row_factory=dict_row, autocommit=autocommit)


@contextmanager
def connection(url: str | None = None, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = connect(url, autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except BaseException:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(conn: psycopg.Connection, sql: str, params=None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn: psycopg.Connection, sql: str, params=None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(conn: psycopg.Connection, sql: str, params=None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount

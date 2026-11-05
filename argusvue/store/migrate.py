"""Plain numbered SQL migrations: migrations/NNNN_name.sql applied in order, each in one transaction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

from argusvue.settings import MIGRATIONS_DIR

_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for p in sorted(migrations_dir.glob("*.sql")):
        m = _NAME_RE.match(p.name)
        if not m:
            raise ValueError(f"migration file name not in NNNN_name.sql form: {p.name}")
        found.append(Migration(int(m.group(1)), m.group(2), p))
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration versions: {versions}")
    return found


def applied_versions(conn: psycopg.Connection) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations(
                   version int PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"""
        )
        cur.execute("SELECT version FROM schema_migrations")
        return {r["version"] for r in cur.fetchall()}


def migrate(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Apply pending migrations. Returns the list applied in this call. Idempotent."""
    done = applied_versions(conn)
    conn.commit()
    applied: list[Migration] = []
    for m in discover(migrations_dir):
        if m.version in done:
            continue
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(m.path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (%s, %s)", (m.version, m.name)
            )
        applied.append(m)
    return applied

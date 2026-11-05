"""Resolve a geo_key to its gazetteer record, ancestors, submarkets and H3 cells."""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from argusvue.geo.keys import GeoKey, GeoKeyError, h3_center, h3_keys, parse_key


@dataclass
class Resolved:
    key: str
    level: str
    name: str | None
    region_id: str | None
    parent_key: str | None
    lat: float | None
    lon: float | None
    ancestors: list[dict] = field(default_factory=list)   # [{geo_key, level, name}] coarse -> fine
    submarkets: list[str] = field(default_factory=list)
    h3: list[str] = field(default_factory=list)
    found: bool = True

    @property
    def ancestor_keys(self) -> list[str]:
        return [a["geo_key"] for a in self.ancestors]

    def ancestor_at(self, level: str) -> str | None:
        for a in self.ancestors:
            if a["level"] == level:
                return a["geo_key"]
        return None


def resolve(conn: psycopg.Connection, key: str) -> Resolved:
    k: GeoKey = parse_key(key)
    if k.level == "h3":
        return _resolve_point(conn, key, *h3_center(k.code))
    with conn.cursor() as cur:
        cur.execute(
            """SELECT geo_key, level::text AS level, name, region_id, parent_key,
                      ST_Y(rep_point) AS lat, ST_X(rep_point) AS lon
               FROM geo_units WHERE geo_key = %s""",
            (key,),
        )
        row = cur.fetchone()
        if row is None:
            raise GeoKeyError(f"{key} is a well-formed key but is not in the gazetteer (run `geo load`)")
        cur.execute(
            """SELECT geo_key, level::text AS level, name FROM geo_ancestors(%s)
               UNION
               SELECT s.geo_key, 'sub', g.name FROM submarkets s JOIN geo_units g ON g.geo_key = s.geo_key
               WHERE %s = ANY(s.member_keys)
               ORDER BY 2, 1""",
            (key, key),
        )
        anc = cur.fetchall()
    anc.sort(key=lambda a: (_rank(a["level"]), a["geo_key"]))
    r = Resolved(key=row["geo_key"], level=row["level"], name=row["name"], region_id=row["region_id"],
                 parent_key=row["parent_key"], lat=row["lat"], lon=row["lon"], ancestors=anc)
    r.submarkets = [a["geo_key"] for a in anc if a["level"] == "sub"]
    if r.lat is not None:
        r.h3 = h3_keys(r.lat, r.lon)
    return r


def _resolve_point(conn: psycopg.Connection, key: str, lat: float, lon: float) -> Resolved:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT geo_key, level::text AS level, name FROM geo_containing(ST_SetSRID(ST_Point(%s, %s), 4326))""",
            (lon, lat),
        )
        anc = cur.fetchall()
    anc.sort(key=lambda a: (_rank(a["level"]), a["geo_key"]))
    region = None
    with conn.cursor() as cur:
        keys = [a["geo_key"] for a in anc if a["level"] == "county"]
        if keys:
            cur.execute("SELECT region_id FROM geo_units WHERE geo_key = %s", (keys[0],))
            row = cur.fetchone()
            region = row["region_id"] if row else None
    r = Resolved(key=key, level=parse_key(key).level, name=None, region_id=region, parent_key=None,
                 lat=lat, lon=lon, ancestors=anc)
    r.submarkets = [a["geo_key"] for a in anc if a["level"] == "sub"]
    r.h3 = h3_keys(lat, lon)
    return r


def _rank(level: str) -> int:
    from argusvue.geo.keys import LEVEL_RANK

    return LEVEL_RANK[level]

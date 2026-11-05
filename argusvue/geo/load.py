"""Gazetteer load for one region: states, CBSAs, counties (national), places/ZCTAs/tracts (region
extent), then submarkets. Idempotent: upserts keyed on geo_key; re-running adds zero rows."""

from __future__ import annotations

import logging
from pathlib import Path

import openpyxl
import psycopg
import shapely
from pyogrio import raw as ogr_raw
from shapely import prepared

from argusvue.config import RegionConfig, load_regions, load_submarkets
from argusvue.config import region as region_cfg
from argusvue.geo import tiger
from argusvue.geo.keys import parse_key
from argusvue.store.jobs import job_run

log = logging.getLogger(__name__)

TIGER_SRID = 4269  # NAD83; stored as 4326

_UPSERT_UNIT = """
INSERT INTO geo_units (geo_key, level, code, name, region_id, geom, vintage, attrs)
VALUES (%(geo_key)s, %(level)s, %(code)s, %(name)s, %(region_id)s,
        ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform(ST_SetSRID(ST_GeomFromWKB(%(wkb)s), 4269), 4326)), 3)),
        %(vintage)s, %(attrs)s::jsonb)
ON CONFLICT (geo_key) DO UPDATE SET
    name = EXCLUDED.name,
    region_id = COALESCE(EXCLUDED.region_id, geo_units.region_id),
    geom = EXCLUDED.geom,
    vintage = EXCLUDED.vintage,
    attrs = geo_units.attrs || EXCLUDED.attrs,
    updated_at = now()
"""


def _read(path: Path, columns: list[str], *, where: str | None = None, bbox=None, geometry: bool = True):
    meta, _fids, geoms, fields = ogr_raw.read(str(path), columns=columns, where=where, bbox=bbox, read_geometry=geometry)
    names = list(meta["fields"])
    cols = {n: fields[i] for i, n in enumerate(names)}
    n = len(cols[names[0]]) if names else 0
    for i in range(n):
        row = {c: (None if cols[c][i] is None else str(cols[c][i])) for c in names}
        yield row, (bytes(geoms[i]) if geometry else None)


def _upsert_many(conn: psycopg.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_UNIT, rows)


def load_delineation(conn: psycopg.Connection, path: Path, vintage: str) -> int:
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = None
    out: list[tuple] = []
    for r in rows:
        if header is None:
            if r and r[0] == "CBSA Code":
                header = list(r)
            continue
        if not r or r[0] is None or not str(r[0]).isdigit():
            continue
        rec = dict(zip(header, r))
        fips = f"{rec['FIPS State Code']}{rec['FIPS County Code']}"
        out.append((fips, str(rec["CBSA Code"]), rec["CBSA Title"], rec["Metropolitan/Micropolitan Statistical Area"],
                    rec.get("Central/Outlying County"), vintage))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO cbsa_counties(county_fips, cbsa_code, cbsa_title, metro_micro, central_outlying, vintage)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (county_fips) DO UPDATE SET cbsa_code=EXCLUDED.cbsa_code, cbsa_title=EXCLUDED.cbsa_title,
                 metro_micro=EXCLUDED.metro_micro, central_outlying=EXCLUDED.central_outlying, vintage=EXCLUDED.vintage""",
            out,
        )
    return len(out)


def verify_region_against_delineation(conn: psycopg.Connection, reg: RegionConfig) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT county_fips, cbsa_title, metro_micro FROM cbsa_counties WHERE cbsa_code = %s", (reg.cbsa,))
        rows = cur.fetchall()
    if not rows:
        raise ValueError(f"region {reg.id}: CBSA {reg.cbsa} not found in delineation file")
    delineated = {r["county_fips"] for r in rows}
    configured = set(reg.counties)
    if delineated != configured:
        raise ValueError(
            f"region {reg.id}: CBSA {reg.cbsa} counties per delineation {sorted(delineated)} "
            f"!= configured {sorted(configured)} (commuter-shed counties belong in commuter_shed_counties)"
        )
    return {"cbsa_title": rows[0]["cbsa_title"], "metro_micro": rows[0]["metro_micro"]}


def _region_extent(county_path: Path, reg: RegionConfig):
    fips = ",".join(f"'{c}'" for c in reg.all_counties)
    geoms = [shapely.from_wkb(wkb) for _row, wkb in _read(county_path, ["GEOID"], where=f"GEOID IN ({fips})")]
    if len(geoms) != len(reg.all_counties):
        raise ValueError(f"region {reg.id}: expected {len(reg.all_counties)} counties in TIGER, found {len(geoms)}")
    return shapely.union_all(geoms)


def load_region(conn: psycopg.Connection, region_id: str) -> dict:
    """Full idempotent gazetteer load for one region. Returns stats."""
    reg = region_cfg(region_id)
    vintage = load_regions().vintage
    with job_run(conn, "geo.load", region_id) as run:
        paths = tiger.ensure_files(vintage.tiger, reg.states)
        stats = run.stats
        year = vintage.tiger.removeprefix("TIGER")

        stats["delineation_rows"] = load_delineation(conn, paths["delineation"], vintage.delineation)
        info = verify_region_against_delineation(conn, reg)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO regions(id, name, cbsa_code, county_fips, config)
                   VALUES (%s,%s,%s,%s,%s::jsonb)
                   ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, cbsa_code=EXCLUDED.cbsa_code,
                     county_fips=EXCLUDED.county_fips, config=EXCLUDED.config, updated_at=now()""",
                (reg.id, info["cbsa_title"], reg.cbsa, reg.all_counties, reg.model_dump_json()),
            )
            cur.execute(
                """INSERT INTO geo_units(geo_key, level, code, name, vintage)
                   VALUES ('country:US','country','US','United States',%s) ON CONFLICT (geo_key) DO NOTHING""",
                (vintage.tiger,),
            )
        conn.commit()

        # States, CBSAs, counties: national coverage (cheap, and lets out-of-region claims resolve coarsely).
        rows = [
            {"geo_key": f"state:{r['GEOID']}", "level": "state", "code": r["GEOID"], "name": r["NAME"],
             "region_id": None, "wkb": wkb, "vintage": vintage.tiger, "attrs": "{}"}
            for r, wkb in _read(paths[f"tl_{year}_us_state.zip"], ["GEOID", "NAME"])
        ]
        _upsert_many(conn, rows)
        stats["states"] = len(rows)

        rows = [
            {"geo_key": f"cbsa:{r['GEOID']}", "level": "cbsa", "code": r["GEOID"], "name": r["NAME"],
             "region_id": reg.id if r["GEOID"] == reg.cbsa else None, "wkb": wkb, "vintage": vintage.tiger,
             "attrs": f'{{"lsad":"{r["LSAD"]}","memi":"{r["MEMI"]}"}}'}
            for r, wkb in _read(paths[f"tl_{year}_us_cbsa.zip"], ["GEOID", "NAME", "LSAD", "MEMI"])
        ]
        _upsert_many(conn, rows)
        stats["cbsas"] = len(rows)
        conn.commit()

        region_counties = set(reg.all_counties)
        rows = [
            {"geo_key": f"county:{r['GEOID']}", "level": "county", "code": r["GEOID"], "name": r["NAMELSAD"],
             "region_id": reg.id if r["GEOID"] in region_counties else None, "wkb": wkb, "vintage": vintage.tiger,
             "attrs": "{}"}
            for r, wkb in _read(paths[f"tl_{year}_us_county.zip"], ["GEOID", "NAMELSAD"])
        ]
        _upsert_many(conn, rows)
        stats["counties"] = len(rows)
        conn.commit()

        # Region extent (union of its counties) drives the place/ZCTA filter.
        extent = _region_extent(paths[f"tl_{year}_us_county.zip"], reg)
        pextent = prepared.prep(extent)
        bbox = tuple(extent.bounds)

        # Places: those whose representative point lies in the region, plus any configured explicitly.
        wanted_places = set(reg.places)
        rows = []
        for st in reg.states:
            for r, wkb in _read(paths[f"tl_{year}_{st}_place.zip"], ["GEOID", "NAME", "NAMELSAD", "LSAD", "CLASSFP"], bbox=bbox):
                g = shapely.from_wkb(wkb)
                if r["GEOID"] in wanted_places or pextent.contains(g.representative_point()):
                    rows.append({"geo_key": f"place:{r['GEOID']}", "level": "place", "code": r["GEOID"],
                                 "name": r["NAMELSAD"], "region_id": reg.id, "wkb": wkb, "vintage": vintage.tiger,
                                 "attrs": f'{{"lsad":"{r["LSAD"]}","classfp":"{r["CLASSFP"]}","short_name":{_json_str(r["NAME"])}}}'})
        loaded_places = {r["code"] for r in rows}
        missing = wanted_places - loaded_places
        if missing:
            raise ValueError(f"region {reg.id}: configured places not found in TIGER: {sorted(missing)}")
        _upsert_many(conn, rows)
        stats["places"] = len(rows)
        conn.commit()

        # ZCTAs: representative point in region, plus any referenced by this region's submarkets.
        sub_zctas = {
            parse_key(m).code
            for s in load_submarkets().submarkets.values() if s.region == reg.id
            for m in s.members if m.startswith("zcta:")
        }
        rows = []
        for r, wkb in _read(paths[f"tl_{year}_us_zcta520.zip"], ["GEOID20"], bbox=bbox):
            g = shapely.from_wkb(wkb)
            if r["GEOID20"] in sub_zctas or pextent.contains(g.representative_point()):
                rows.append({"geo_key": f"zcta:{r['GEOID20']}", "level": "zcta", "code": r["GEOID20"],
                             "name": f"ZCTA5 {r['GEOID20']}", "region_id": reg.id, "wkb": wkb,
                             "vintage": vintage.tiger, "attrs": "{}"})
        loaded_z = {r["code"] for r in rows}
        missing_z = sub_zctas - loaded_z
        if missing_z:
            raise ValueError(f"region {reg.id}: submarket ZCTAs not found in TIGER (within region bbox): {sorted(missing_z)}")
        _upsert_many(conn, rows)
        stats["zctas"] = len(rows)
        conn.commit()

        # Tracts: by county code.
        rows = []
        for st in reg.states:
            counties_in_state = [c for c in reg.all_counties if c.startswith(st)]
            if not counties_in_state:
                continue
            where = " OR ".join(f"GEOID LIKE '{c}%'" for c in counties_in_state)
            for r, wkb in _read(paths[f"tl_{year}_{st}_tract.zip"], ["GEOID", "NAMELSAD"], where=where):
                rows.append({"geo_key": f"tract:{r['GEOID']}", "level": "tract", "code": r["GEOID"],
                             "name": r["NAMELSAD"], "region_id": reg.id, "wkb": wkb, "vintage": vintage.tiger, "attrs": "{}"})
        _upsert_many(conn, rows)
        stats["tracts"] = len(rows)
        conn.commit()

        stats.update(derive_attributes(conn))
        stats.update(load_submarkets_for_region(conn, reg.id))
        conn.commit()
        return dict(stats)


def _json_str(s: str) -> str:
    import json

    return json.dumps(s)


def derive_attributes(conn: psycopg.Connection) -> dict:
    """Representative points, areas, parent keys and region ids for every unit. Safe to re-run."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE geo_units SET rep_point = ST_PointOnSurface(geom), area_m2 = ST_Area(geom::geography)
               WHERE geom IS NOT NULL AND (rep_point IS NULL OR updated_at > created_at)"""
        )
        # Parent keys. Structural where the code defines it; containment otherwise.
        cur.execute("UPDATE geo_units SET parent_key = 'country:US' WHERE level = 'state' AND parent_key IS DISTINCT FROM 'country:US'")
        cur.execute(
            """UPDATE geo_units c SET parent_key = 'county:' || substr(c.code, 1, 5)
               WHERE c.level IN ('tract','parcel')
                 AND EXISTS (SELECT 1 FROM geo_units p WHERE p.geo_key = 'county:' || substr(c.code, 1, 5))
                 AND c.parent_key IS DISTINCT FROM 'county:' || substr(c.code, 1, 5)"""
        )
        cur.execute(
            """UPDATE geo_units c SET parent_key = COALESCE('cbsa:' || d.cbsa_code, 'state:' || substr(c.code, 1, 2))
               FROM (SELECT g.geo_key, g.code, cc.cbsa_code FROM geo_units g
                     LEFT JOIN cbsa_counties cc ON cc.county_fips = g.code
                     LEFT JOIN geo_units cb ON cb.geo_key = 'cbsa:' || cc.cbsa_code
                     WHERE g.level = 'county') d
               WHERE c.geo_key = d.geo_key
                 AND c.parent_key IS DISTINCT FROM COALESCE('cbsa:' || d.cbsa_code, 'state:' || substr(c.code, 1, 2))"""
        )
        cur.execute(
            """UPDATE geo_units c SET parent_key = p.geo_key
               FROM geo_units p
               WHERE c.level = 'cbsa' AND p.level = 'state' AND ST_Contains(p.geom, c.rep_point)
                 AND c.parent_key IS DISTINCT FROM p.geo_key"""
        )
        cur.execute(
            """UPDATE geo_units c SET parent_key = p.geo_key, region_id = COALESCE(c.region_id, p.region_id)
               FROM geo_units p
               WHERE c.level IN ('place','zcta','sub') AND p.level = 'county' AND ST_Contains(p.geom, c.rep_point)
                 AND (c.parent_key IS DISTINCT FROM p.geo_key OR c.region_id IS NULL)"""
        )
        cur.execute("SELECT count(*) AS n FROM geo_units WHERE level NOT IN ('country') AND parent_key IS NULL")
        orphans = cur.fetchone()["n"]
    return {"orphans_without_parent": orphans}


def load_submarkets_for_region(conn: psycopg.Connection, region_id: str) -> dict:
    """Verify every member key exists, then upsert submarkets (also as geo_units level 'sub')."""
    subs = [s for s in load_submarkets().submarkets.values() if s.region == region_id]
    n = 0
    with conn.cursor() as cur:
        for s in subs:
            cur.execute("SELECT geo_key FROM geo_units WHERE geo_key = ANY(%s)", (s.members,))
            found = {r["geo_key"] for r in cur.fetchall()}
            missing = set(s.members) - found
            if missing:
                raise ValueError(f"submarket {s.id}: member keys not in gazetteer: {sorted(missing)}")
            cur.execute(
                """INSERT INTO geo_units(geo_key, level, code, name, region_id, geom, vintage)
                   SELECT %s, 'sub', %s, %s, %s, ST_Multi(ST_Union(geom)), 'config'
                   FROM geo_units WHERE geo_key = ANY(%s)
                   ON CONFLICT (geo_key) DO UPDATE SET name=EXCLUDED.name, geom=EXCLUDED.geom,
                     region_id=EXCLUDED.region_id, updated_at=now()""",
                (s.geo_key, s.id, s.name, region_id, s.members),
            )
            cur.execute(
                """INSERT INTO submarkets(geo_key, region_id, name, member_keys, geom)
                   SELECT geo_key, %s, %s, %s, geom FROM geo_units WHERE geo_key = %s
                   ON CONFLICT (geo_key) DO UPDATE SET name=EXCLUDED.name, member_keys=EXCLUDED.member_keys,
                     geom=EXCLUDED.geom, region_id=EXCLUDED.region_id""",
                (region_id, s.name, s.members, s.geo_key),
            )
            n += 1
        cur.execute(
            """UPDATE geo_units SET rep_point = ST_PointOnSurface(geom), area_m2 = ST_Area(geom::geography)
               WHERE level = 'sub' AND region_id = %s""",
            (region_id,),
        )
        cur.execute(
            """UPDATE geo_units c SET parent_key = p.geo_key FROM geo_units p
               WHERE c.level = 'sub' AND c.region_id = %s AND p.level = 'county' AND ST_Contains(p.geom, c.rep_point)""",
            (region_id,),
        )
    return {"submarkets": n}

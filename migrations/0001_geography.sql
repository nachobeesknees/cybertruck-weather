-- Phase 0: geography + run logging.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

-- Enum order is the hierarchy order (broad -> fine); Postgres compares enums by declaration order.
-- 'h3' is last and is never derived by containment (cells come from the representative point).
CREATE TYPE geo_level AS ENUM ('country','state','cbsa','county','place','sub','zcta','tract','parcel','h3');

CREATE TABLE regions (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    cbsa_code     text NOT NULL,
    county_fips   text[] NOT NULL,
    config        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE geo_units (
    geo_key     text PRIMARY KEY,
    level       geo_level NOT NULL,
    code        text NOT NULL,
    name        text,
    parent_key  text REFERENCES geo_units(geo_key),
    region_id   text REFERENCES regions(id),
    geom        geometry(MultiPolygon, 4326),
    rep_point   geometry(Point, 4326),          -- ST_PointOnSurface(geom); the point used for containment
    area_m2     double precision,
    vintage     text,                           -- e.g. 'TIGER2024'
    attrs       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (level, code)
);
CREATE INDEX geo_units_geom_gix ON geo_units USING GIST (geom);
CREATE INDEX geo_units_rep_point_gix ON geo_units USING GIST (rep_point);
CREATE INDEX geo_units_level_idx ON geo_units (level);
CREATE INDEX geo_units_region_idx ON geo_units (region_id);

-- OMB/Census delineation file: CBSA composition by county.
CREATE TABLE cbsa_counties (
    county_fips      text PRIMARY KEY,
    cbsa_code        text NOT NULL,
    cbsa_title       text NOT NULL,
    metro_micro      text NOT NULL,
    central_outlying text,
    vintage          text NOT NULL
);
CREATE INDEX cbsa_counties_cbsa_idx ON cbsa_counties (cbsa_code);

CREATE TABLE submarkets (
    geo_key     text PRIMARY KEY REFERENCES geo_units(geo_key),
    region_id   text NOT NULL REFERENCES regions(id),
    name        text NOT NULL,
    member_keys text[] NOT NULL,
    geom        geometry(MultiPolygon, 4326) NOT NULL
);

CREATE TABLE job_runs (
    id          bigserial PRIMARY KEY,
    job         text NOT NULL,
    region_id   text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    ok          boolean,
    stats       jsonb NOT NULL DEFAULT '{}'::jsonb,
    error       text
);
CREATE INDEX job_runs_job_idx ON job_runs (job, started_at DESC);

-- Units containing a point, coarsest first. 'country' is the universal ancestor (no geometry needed).
CREATE OR REPLACE FUNCTION geo_containing(p geometry)
RETURNS TABLE (geo_key text, level geo_level, name text) LANGUAGE sql STABLE AS $$
    SELECT g.geo_key, g.level, g.name
    FROM geo_units g
    WHERE g.level = 'country'
       OR (g.level NOT IN ('country', 'h3') AND g.geom IS NOT NULL AND ST_Contains(g.geom, p))
    ORDER BY g.level, g.geo_key
$$;

-- Ancestors of a stored unit: every coarser unit containing its representative point.
CREATE OR REPLACE FUNCTION geo_ancestors(p_key text)
RETURNS TABLE (geo_key text, level geo_level, name text) LANGUAGE sql STABLE AS $$
    SELECT a.geo_key, a.level, a.name
    FROM geo_units c
    JOIN LATERAL geo_containing(c.rep_point) a ON a.level < c.level
    WHERE c.geo_key = p_key
$$;

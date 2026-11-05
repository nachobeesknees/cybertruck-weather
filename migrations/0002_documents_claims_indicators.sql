-- Phase 1: sources, documents, entities, claims, indicators. Append-only by design: claims are never
-- edited except for status/supersession links; indicator_points keep every revision.

CREATE TYPE source_kind AS ENUM ('structured','web_monitor','web_task','own_fetch','pdf','feed','search');
CREATE TYPE entity_type AS ENUM ('place','parcel','project','org','person','instrument','event');
CREATE TYPE entity_status AS ENUM ('candidate','canonical','merged');
CREATE TYPE claim_status AS ENUM ('asserted','corroborated','contested','superseded','retracted');

CREATE TABLE sources (
    id            serial PRIMARY KEY,
    slug          text NOT NULL UNIQUE,
    name          text NOT NULL,
    kind          source_kind NOT NULL,
    region_id     text REFERENCES regions(id),          -- NULL: national / all regions
    url           text,
    cadence       interval,
    reliability   numeric NOT NULL DEFAULT 0.5 CHECK (reliability >= 0 AND reliability <= 1),
    license_note  text,
    origin_key    text NOT NULL,                         -- canonical origin for independence counting
    active        boolean NOT NULL DEFAULT true,
    config        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
    id            bigserial PRIMARY KEY,
    source_id     int REFERENCES sources(id),
    url           text NOT NULL,
    fetched_at    timestamptz NOT NULL,                 -- first time this exact content was observed
    content_hash  bytea NOT NULL UNIQUE,                -- sha256 of the raw bytes
    simhash       bigint,
    title         text,
    published_at  timestamptz,
    blob_ref      text NOT NULL,
    mime          text,
    size_bytes    bigint,
    text_len      int,
    origin_key    text,
    meta          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX documents_source_idx ON documents (source_id, fetched_at DESC);
CREATE INDEX documents_url_idx ON documents (url);

-- Which (document, source, region) combinations have been fully ingested; lets re-runs skip parsing.
CREATE TABLE document_ingests (
    document_id   bigint NOT NULL REFERENCES documents(id),
    source_id     int NOT NULL REFERENCES sources(id),
    region_id     text NOT NULL,                        -- '*' for national context
    points        int NOT NULL DEFAULT 0,
    claims        int NOT NULL DEFAULT 0,
    finished_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, source_id, region_id)
);

CREATE TABLE entities (
    id             bigserial PRIMARY KEY,
    type           entity_type NOT NULL,
    canonical_name text NOT NULL,
    region_id      text REFERENCES regions(id),
    geo_key        text,
    geom           geometry(Geometry, 4326),
    status         entity_status NOT NULL DEFAULT 'candidate',
    merged_into    bigint REFERENCES entities(id),
    attrs          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entities_type_name_idx ON entities (type, lower(canonical_name));
CREATE INDEX entities_geo_idx ON entities (geo_key);

CREATE TABLE entity_ids (
    entity_id  bigint NOT NULL REFERENCES entities(id),
    scheme     text NOT NULL,     -- geo_key, APN, FIPS, CBSA, ZCTA, CIK, EIN, OSM, AZ_SOS, WY_SOS, ...
    value      text NOT NULL,
    PRIMARY KEY (scheme, value)
);
CREATE INDEX entity_ids_entity_idx ON entity_ids (entity_id);

CREATE TABLE claims (
    id                  bigserial PRIMARY KEY,
    subject_entity      bigint NOT NULL REFERENCES entities(id),
    predicate           text NOT NULL,
    object_entity       bigint REFERENCES entities(id),
    value_num           numeric,
    value_text          text,
    unit                text,
    geo_key             text NOT NULL,
    valid_from          date,
    valid_to            date,
    observed_at         timestamptz NOT NULL,
    document_id         bigint NOT NULL REFERENCES documents(id),
    excerpt_start       int,
    excerpt_end         int,
    excerpt             text NOT NULL,
    extractor_version   text NOT NULL,
    source_reliability  numeric NOT NULL,
    extraction_conf     numeric NOT NULL,
    corroboration_count int NOT NULL DEFAULT 1,
    confidence          numeric NOT NULL,
    status              claim_status NOT NULL DEFAULT 'asserted',
    half_life_days      int,
    superseded_by       bigint REFERENCES claims(id),
    attrs               jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX claims_subject_pred_idx ON claims (subject_entity, predicate, valid_from);
CREATE INDEX claims_pred_geo_idx ON claims (predicate, geo_key, valid_from);
CREATE INDEX claims_document_idx ON claims (document_id);
CREATE INDEX claims_observed_idx ON claims (observed_at);
CREATE INDEX claims_status_idx ON claims (status);

CREATE TABLE entity_links (
    id          bigserial PRIMARY KEY,
    src_entity  bigint NOT NULL REFERENCES entities(id),
    predicate   text NOT NULL,
    dst_entity  bigint NOT NULL REFERENCES entities(id),
    claim_id    bigint REFERENCES claims(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE claim_geo (
    claim_id  bigint NOT NULL REFERENCES claims(id),
    geo_key   text NOT NULL,
    level     geo_level NOT NULL,
    PRIMARY KEY (claim_id, geo_key)
);
CREATE INDEX claim_geo_key_idx ON claim_geo (geo_key, level);

CREATE TABLE claim_origins (
    claim_id    bigint NOT NULL REFERENCES claims(id),
    origin_key  text NOT NULL,
    PRIMARY KEY (claim_id, origin_key)
);

CREATE TABLE indicators (
    id              text PRIMARY KEY,
    name            text NOT NULL,
    unit            text NOT NULL,
    direction_good  text NOT NULL CHECK (direction_good IN ('up','down','neutral')),
    half_life_days  int NOT NULL,
    source_id       int REFERENCES sources(id),
    region_scope    text[] NOT NULL,
    frequency       text NOT NULL CHECK (frequency IN ('daily','weekly','monthly','quarterly','annual')),
    description     text,
    low_confidence  boolean NOT NULL DEFAULT false,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE indicator_points (
    indicator_id  text NOT NULL REFERENCES indicators(id),
    geo_key       text NOT NULL,
    period        date NOT NULL,
    value         numeric NOT NULL,
    claim_id      bigint NOT NULL REFERENCES claims(id),
    observed_at   timestamptz NOT NULL,
    PRIMARY KEY (indicator_id, geo_key, period, observed_at)
);
CREATE INDEX indicator_points_series_idx ON indicator_points (indicator_id, geo_key, period DESC, observed_at DESC);

-- Current belief per series point: the most recently observed revision.
CREATE VIEW indicator_latest AS
    SELECT DISTINCT ON (indicator_id, geo_key, period)
           indicator_id, geo_key, period, value, claim_id, observed_at
    FROM indicator_points
    ORDER BY indicator_id, geo_key, period, observed_at DESC;

-- Materialised ancestry (coarser containers + explicit submarket membership) for bulk claim_geo fills.
-- Refreshed by `geo load`; geo_ancestors() stays the dynamic reference implementation.
CREATE TABLE geo_ancestry (
    geo_key       text NOT NULL REFERENCES geo_units(geo_key),
    ancestor_key  text NOT NULL REFERENCES geo_units(geo_key),
    level         geo_level NOT NULL,
    PRIMARY KEY (geo_key, ancestor_key)
);

CREATE OR REPLACE FUNCTION geo_ancestry_refresh() RETURNS int LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
    DELETE FROM geo_ancestry;
    INSERT INTO geo_ancestry (geo_key, ancestor_key, level)
    SELECT c.geo_key, 'country:US', 'country' FROM geo_units c
    WHERE c.level <> 'country' AND EXISTS (SELECT 1 FROM geo_units WHERE geo_key = 'country:US')
    UNION
    SELECT c.geo_key, a.geo_key, a.level
    FROM geo_units c JOIN geo_units a
      ON a.level < c.level AND a.level NOT IN ('country','h3') AND a.geom IS NOT NULL
     AND c.rep_point IS NOT NULL AND ST_Contains(a.geom, c.rep_point)
    UNION
    SELECT m.geo_key, s.geo_key, 'sub'
    FROM submarkets s JOIN LATERAL unnest(s.member_keys) AS m(geo_key) ON true
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- Phase 4: theses, versions, evidence links, causal edges, falsifiers, human gate, quant outputs.
CREATE TYPE thesis_status AS ENUM ('emerging','established','weakening','dead');
CREATE TYPE human_status AS ENUM ('pending','approved','rejected');
CREATE TYPE claim_role AS ENUM ('support','counter');
CREATE TYPE edge_status AS ENUM ('hypothesis','tested');

CREATE TABLE theses (
    id               bigserial PRIMARY KEY,
    slug             text NOT NULL UNIQUE,
    region_id        text NOT NULL REFERENCES regions(id),
    geo_key          text NOT NULL,
    domain           text NOT NULL,
    parent_id        bigint REFERENCES theses(id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    current_version  int NOT NULL DEFAULT 0,       -- latest written version
    approved_version int NOT NULL DEFAULT 0,       -- latest version whose status change a human approved
    human_status     human_status NOT NULL DEFAULT 'pending',
    active           boolean NOT NULL DEFAULT true,
    last_reviewed_at timestamptz
);

CREATE TABLE thesis_versions (
    thesis_id            bigint NOT NULL REFERENCES theses(id),
    version              int NOT NULL,
    statement            text NOT NULL,
    status               thesis_status NOT NULL,
    confidence           numeric NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    confidence_reasoning jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {independence, recency, corroboration, mechanism, base_rate}
    implications         jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{text, cites[], horizon_months, hypothesis}]
    change_summary       text NOT NULL,
    analyst_run_id       bigint REFERENCES agent_runs(id),
    critic_run_id        bigint REFERENCES agent_runs(id),
    critic_verdict       text,                                 -- approve | revise | reject | seed
    known_disputes       jsonb NOT NULL DEFAULT '[]'::jsonb,
    trigger              text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thesis_id, version)
);

CREATE TABLE thesis_claims (
    thesis_id  bigint NOT NULL,
    version    int NOT NULL,
    claim_id   bigint NOT NULL REFERENCES claims(id),
    role       claim_role NOT NULL,
    PRIMARY KEY (thesis_id, version, claim_id, role),
    FOREIGN KEY (thesis_id, version) REFERENCES thesis_versions(thesis_id, version)
);

CREATE TABLE causal_edges (
    id                  bigserial PRIMARY KEY,
    thesis_id           bigint NOT NULL,
    version             int NOT NULL,
    cause               text NOT NULL,
    effect              text NOT NULL,
    mechanism           text NOT NULL,
    lag_months          int NOT NULL DEFAULT 0,
    strength            text NOT NULL CHECK (strength IN ('weak','moderate','strong')),
    evidence_claim_ids  bigint[] NOT NULL DEFAULT '{}',
    cause_indicator     text REFERENCES indicators(id),     -- optional: for lead-lag tests
    effect_indicator    text REFERENCES indicators(id),
    geo_key             text,
    status              edge_status NOT NULL DEFAULT 'hypothesis',
    test_result         jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (thesis_id, version) REFERENCES thesis_versions(thesis_id, version)
);

CREATE TABLE falsifiers (
    id            bigserial PRIMARY KEY,
    thesis_id     bigint NOT NULL,
    version       int NOT NULL,
    indicator_id  text NOT NULL REFERENCES indicators(id),
    geo_key       text NOT NULL,
    operator      text NOT NULL CHECK (operator IN ('<','>','<=','>=')),
    threshold     numeric NOT NULL,
    by_date       date NOT NULL,
    action        text NOT NULL CHECK (action IN ('downgrade','retire','review')),
    description   text,
    baseline      jsonb NOT NULL DEFAULT '{}'::jsonb,          -- how the threshold was set
    resolved_at   timestamptz,                                  -- when the outcome became final (triggered or expired)
    outcome       text CHECK (outcome IN ('triggered','expired')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (thesis_id, version) REFERENCES thesis_versions(thesis_id, version)
);

CREATE TABLE falsifier_checks (
    id            bigserial PRIMARY KEY,
    falsifier_id  bigint NOT NULL REFERENCES falsifiers(id),
    checked_at    timestamptz NOT NULL DEFAULT now(),
    period        date,
    value         numeric,
    triggered     boolean NOT NULL
);
CREATE INDEX falsifier_checks_f_idx ON falsifier_checks (falsifier_id, checked_at DESC);

CREATE TABLE review_decisions (
    id          bigserial PRIMARY KEY,
    thesis_id   bigint NOT NULL REFERENCES theses(id),
    version     int NOT NULL,
    decision    text NOT NULL CHECK (decision IN ('approved','rejected')),
    note        text,
    decided_at  timestamptz NOT NULL DEFAULT now()
);

-- Every step of a review (trigger, triage, analyst, validator, critic, gate) with its run id and payload.
CREATE TABLE thesis_events (
    id          bigserial PRIMARY KEY,
    thesis_id   bigint NOT NULL REFERENCES theses(id),
    version     int,
    kind        text NOT NULL,
    run_id      bigint REFERENCES agent_runs(id),
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX thesis_events_thesis_idx ON thesis_events (thesis_id, created_at DESC);

-- Quant outputs: anomalies per series point (history kept per computation).
CREATE TABLE indicator_anomalies (
    indicator_id  text NOT NULL REFERENCES indicators(id),
    geo_key       text NOT NULL,
    period        date NOT NULL,
    value         numeric NOT NULL,
    zscore        numeric,            -- vs trailing 36 periods
    yoy_pct       numeric,
    mom_pct       numeric,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (indicator_id, geo_key, period, computed_at)
);
CREATE INDEX indicator_anomalies_latest_idx ON indicator_anomalies (indicator_id, geo_key, period DESC, computed_at DESC);

-- The version the renderer treats as current: the approved one, unless the latest needs no human gate.
CREATE VIEW thesis_current AS
    SELECT t.id, t.slug, t.region_id, t.geo_key, t.domain, t.human_status, t.current_version, t.approved_version,
           v.version AS rendered_version, v.statement, v.status, v.confidence, v.change_summary, v.created_at AS version_at,
           (t.current_version <> t.approved_version) AS pending_human
    FROM theses t
    JOIN thesis_versions v ON v.thesis_id = t.id AND v.version = t.approved_version
    WHERE t.active;

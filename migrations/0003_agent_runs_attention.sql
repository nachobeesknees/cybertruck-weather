-- Phase 2: paid-call ledger (LLM + search), attention ledger for monitors, open questions.

CREATE TABLE agent_runs (
    id                 bigserial PRIMARY KEY,
    role               text NOT NULL,            -- triage, extraction, resolution, entailment, scout, analyst, critic, search
    model              text NOT NULL,
    prompt_hash        text,                     -- sha256 prefix of the system prompt (prompt versioning)
    region_id          text,
    geo_key            text,
    input_ref          text,                     -- blob ref of the full request packet
    output_ref         text,                     -- blob ref of the raw model output
    input_tokens       int NOT NULL DEFAULT 0,
    output_tokens      int NOT NULL DEFAULT 0,
    cache_read_tokens  int NOT NULL DEFAULT 0,
    cache_write_tokens int NOT NULL DEFAULT 0,
    search_uses        int NOT NULL DEFAULT 0,
    cost_usd           numeric(12, 6) NOT NULL DEFAULT 0,
    latency_ms         int,
    ok                 boolean NOT NULL,
    error              text,
    attempt            int NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_runs_created_idx ON agent_runs (created_at DESC);
CREATE INDEX agent_runs_role_idx ON agent_runs (role, created_at DESC);

-- Spend by local day (America/Phoenix), role and model.
CREATE VIEW spend_daily AS
    SELECT (created_at AT TIME ZONE 'America/Phoenix')::date AS day, role, model,
           count(*) AS calls, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
           sum(search_uses) AS search_uses, sum(cost_usd) AS cost_usd
    FROM agent_runs GROUP BY 1, 2, 3;

CREATE TABLE attention_ledger (
    source_id        int NOT NULL REFERENCES sources(id),
    target           text NOT NULL,              -- feed URL, page URL, or search objective
    last_checked_at  timestamptz,
    last_result      text,                       -- 'new:3', 'nothing_new', 'error: ...', 'robots_disallowed'
    next_due_at      timestamptz,
    checks           int NOT NULL DEFAULT 0,
    new_docs         int NOT NULL DEFAULT 0,
    last_error_at    timestamptz,
    PRIMARY KEY (source_id, target)
);
CREATE INDEX attention_ledger_due_idx ON attention_ledger (next_due_at);

CREATE TABLE open_questions (
    id                       bigserial PRIMARY KEY,
    region_id                text REFERENCES regions(id),
    geo_key                  text,
    question                 text NOT NULL,
    suggested_collector_task jsonb,
    raised_by_run            bigint REFERENCES agent_runs(id),
    status                   text NOT NULL DEFAULT 'open' CHECK (status IN ('open','scheduled','answered','dropped')),
    created_at               timestamptz NOT NULL DEFAULT now(),
    resolved_at              timestamptz
);

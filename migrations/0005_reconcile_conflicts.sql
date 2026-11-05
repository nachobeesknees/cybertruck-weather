-- Phase 3: reconciliation bookkeeping and conflicts.
ALTER TABLE claims ADD COLUMN reconciled_at timestamptz;
CREATE INDEX claims_unreconciled_idx ON claims (id) WHERE reconciled_at IS NULL;

CREATE TABLE conflicts (
    id              bigserial PRIMARY KEY,
    claim_a         bigint NOT NULL REFERENCES claims(id),
    claim_b         bigint NOT NULL REFERENCES claims(id),
    kind            text NOT NULL,                  -- contradiction | numeric_disagreement | status_disagreement
    opened_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    resolution      text,                           -- keep_a | keep_b | both_valid | retract_both | unresolved
    resolver_run_id bigint REFERENCES agent_runs(id),
    attempts        int NOT NULL DEFAULT 0,
    notes           text,
    region_id       text,
    geo_key         text,
    UNIQUE (claim_a, claim_b)
);
CREATE INDEX conflicts_open_idx ON conflicts (opened_at) WHERE resolved_at IS NULL;

-- effective confidence with half-life decay (SPEC §6)
CREATE OR REPLACE FUNCTION claim_effective_confidence(p_confidence numeric, p_observed_at timestamptz, p_half_life_days int,
                                                      p_as_of timestamptz DEFAULT now())
RETURNS numeric LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN p_half_life_days IS NULL OR p_half_life_days <= 0 THEN p_confidence
                ELSE p_confidence * power(0.5, GREATEST(EXTRACT(EPOCH FROM (p_as_of - p_observed_at)) / 86400.0, 0) / p_half_life_days)
           END
$$;

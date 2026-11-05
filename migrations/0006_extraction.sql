-- Phase 3: extraction bookkeeping.
CREATE TABLE document_extractions (
    document_id   bigint NOT NULL REFERENCES documents(id),
    prompt_hash   text NOT NULL,
    run_ids       bigint[] NOT NULL DEFAULT '{}',
    status        text NOT NULL CHECK (status IN ('done','skipped','failed')),
    claims        int NOT NULL DEFAULT 0,
    rejected      int NOT NULL DEFAULT 0,
    reasons       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, prompt_hash)
);
ALTER TABLE claims ADD COLUMN extraction_run_id bigint REFERENCES agent_runs(id);
CREATE INDEX entities_region_type_idx ON entities (region_id, type, status);

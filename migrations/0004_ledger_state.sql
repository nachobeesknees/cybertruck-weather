-- Per-target monitor state (last feed hash, last seen entry ids, cursors).
ALTER TABLE attention_ledger ADD COLUMN state jsonb NOT NULL DEFAULT '{}'::jsonb;

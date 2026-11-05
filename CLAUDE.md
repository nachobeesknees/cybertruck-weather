# Argusvue — working notes for Claude Code

Argusvue is a "living synthesis layer": a continuously updated, source-traceable model of a
place's real estate market and regional economy that explains its reasoning with citations.
Pilot regions: `phx` (metro Phoenix, AZ) and `jh` (Jackson, WY + commuter shed).
The full brief is `docs/SPEC.md`; read it before changing anything. Plan: `docs/PLAN.md`.
Deviations and fallbacks: `docs/DECISIONS.md`. Demo commands: `docs/DEMO.md`. Status: `docs/PROGRESS.md`.

## Principles (non-negotiable)

- Nothing is overwritten. Claims and thesis versions are immutable; "current belief" is a view.
- Every rendered sentence that states a fact carries claim or indicator ids. The renderer refuses sentences that don't.
- Geography is a parameter of every job, never an agent's identity. Same code runs both regions from config.
- LLMs propose, code validates, statistics grade.
- Bi-temporal ledger (valid time vs observed time) so "what did we believe on date X" is answerable.
- Respect robots.txt and site terms. Never scrape paywalled or licensed content. Cite public brokerage/market reports; don't republish their tables.

## Stack (decided)

- Python 3.12, `uv`, `pydantic` v2, `typer`, `httpx`, `trafilatura`, `pdfplumber`, `h3`, `shapely`, `psycopg` v3.
- Postgres 16 + PostGIS + pgvector via `docker compose`. Numbered SQL migrations in `migrations/`, applied by a small runner. No ORM; parameterised SQL in a thin repository layer.
- APScheduler in-process. Every job is idempotent; re-running never duplicates rows. Every run recorded in `job_runs`.
- Open-web collection: our own agents behind the `Collector` interface: `own_search` (Claude web search/fetch via `llm.py`), `feed_monitor` (RSS/Atom + scheduled refetch), `own_fetch` (allowlisted URLs). Parallel and Exa are optional stubbed adapters, enabled only if their key is set (DECISIONS 2026-11-05).
- LLM calls go through `argusvue/llm.py` only. Models per role in `config/models.yaml` (triage haiku, extraction/resolution sonnet, analyst/critic opus). Every call logged to `agent_runs`.
- Blob storage: `data/snapshots/` behind a `BlobStore` with an S3-compatible interface.
- Secrets in `.env` (`ANTHROPIC_API_KEY`, `DATABASE_URL`, `TEST_DATABASE_URL`, `DAILY_SPEND_CAP_USD`=20; optional `BRAVE_API_KEY`, `PARALLEL_API_KEY`, `EXA_API_KEY`). Never commit `.env`.
- Spend guard in `llm.py` and collector adapters; when the cap is hit, jobs stop cleanly and the brief reports it.

## Dev environment

- `uv sync`, then `uv run argusvue …` and `uv run pytest -q`. Lint: `uv run ruff check argusvue tests`.
- Tests: pure unit tests always run; `db`-marked tests need `TEST_DATABASE_URL`; `gazetteer`-marked tests need both regions loaded.
- Migrations are `migrations/NNNN_name.sql`; never edit an applied one, add a new file.

## Working rules

1. Work one phase at a time (SPEC §11). Don't start the next until acceptance criteria pass and `docs/DEMO.md` has been seen by the owner.
2. Tests before features for anything with rules: geo derivation, dedup, reconcile cases, decay math, validator, renderer tag enforcement, spend guard. Golden-file tests for prompts.
3. Every LLM output is parsed into a pydantic model; on failure, re-prompt once with the validation errors, then fail loudly. No silent fallbacks.
4. Never scrape paywalled or licensed sources; honour robots.txt; descriptive User-Agent with a contact URL, not an email.
5. Keep `docs/DECISIONS.md` current: every deviation from the spec, every unavailable source and its fallback.
6. Stay under `DAILY_SPEND_CAP_USD`; prefer haiku/sonnet wherever allowed; batch search/fetch runs.
7. Each phase ends with tests green, `DEMO.md` and `PROGRESS.md` updated, and one commit summarising what works and what's deferred.
8. Unanswered plan questions resolve to SPEC §13 defaults; record the choice in `DECISIONS.md`.

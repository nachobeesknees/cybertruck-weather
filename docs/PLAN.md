# Argusvue — PLAN

Status: **phase 0 complete (2026-11-05).** Questions in §3 were resolved by their defaults; the owner chose
own collection agents over Parallel/Exa. Both recorded in `docs/DECISIONS.md`. Next: phase 1.

## 1. My understanding

Argusvue is a backend that turns public data about a place into an auditable belief system.
Four layers, each append-only:

1. **Documents.** Structured downloads (Redfin, Zillow, BLS, Census, state revenue, NPS, SNOTEL…)
   and open-web pages/PDFs (via our own agents: Claude web search, feed monitors, an allowlisted own
   fetcher; Parallel/Exa kept as optional adapters) are snapshotted to a
   `BlobStore`, hashed, near-dup'd (simhash), and tagged with an `origin_key` so syndicated copies
   count as one source.
2. **Claims.** Typed `(subject, predicate, value, geo_key, valid_from/to, observed_at)` facts with a
   verbatim excerpt from exactly one document. Structured rows become synthetic claims too, so every
   indicator point is traceable to a download. Reconciliation corroborates, merges, contests or
   supersedes; nothing is edited in place. Confidence decays by predicate half-life.
3. **Theses.** Per `(region, geo_key, domain)` versioned beliefs with support/counter claims, causal
   edges, implications and machine-checkable falsifiers. Updated only through
   Triage (haiku) → Analyst (opus) → Validator (code + entailment) → Critic (opus) → gate, and a human
   `approve` is required before any status change becomes "current".
4. **Rendering.** Daily brief per region and a page per place, every factual sentence tagged with
   `[c:id]` / `[i:indicator:period]`; untagged sentences are dropped and logged.

Cross-cutting: geography is a `geo_key` hierarchy (`country → state → cbsa → county → place → sub →
zcta → tract → parcel`, plus H3) loaded from TIGER/Line and the OMB delineation file, with ancestors
materialised on write via PostGIS containment. Both regions run from config with no special-casing.
Everything the LLM says is parsed into pydantic models against an id allowlist; statistics (z-scores,
lead-lag, falsifier checks, Brier) grade the result. Bi-temporal storage (`valid_*` vs `observed_at`)
makes `replay --as-of` possible.

Things I'll treat as fixed from the spec: the DDL sketch (refined, not diluted), the CLI surface,
the model-per-role table, the reconcile and decay formulas, the seed submarkets and theses, and the
phase order with its acceptance criteria.

## 2. Phase 0 task list — scaffold + geography

Order is tests-first where there are rules (key parsing, ancestor derivation, containment).

1. **Repo scaffold.** `pyproject.toml` (uv, Python 3.12, pinned deps), `argusvue/` package layout
   from SPEC §2, `.env.example`, `.gitignore` (`data/`, `out/`, `.env`), `docker-compose.yml`
   (postgis/postgis:16-3.4 + pgvector), `README` pointer to docs.
2. **DB layer.** `store/db.py` (psycopg v3 pool from `DATABASE_URL`), migration runner
   (`migrations/0001_*.sql` …, `schema_migrations` table, idempotent, applied in order),
   `argusvue db migrate`. Migration 0001 creates the geography tables (`regions`, `geo_units`,
   `submarkets`) and the run-logging tables (`job_runs`) so phase 0 jobs are already recorded.
   Remaining tables arrive with the phase that uses them.
3. **Config.** `config/regions.yaml` (phx, jh: CBSA code, county FIPS, places of interest by
   FIPS place code, commuter-shed counties flagged), `config/submarkets.yaml` (seed ZCTA unions),
   `config/models.yaml`. Pydantic loaders with strict validation. Names are never hardcoded;
   codes resolve to names from the gazetteer.
4. **geo_key.** `geo/keys.py`: parse/format `{level}:{code}`, level ordering, validation per level
   (FIPS lengths, ZCTA 5 digits, H3 cell validity, parcel `county:APN`). Unit tests.
5. **Gazetteer load.** `geo/tiger.py`: download TIGER/Line 2024 shapefiles (state, CBSA, county,
   place, ZCTA5, tract) into `data/downloads/`, filter to the region's counties plus their ancestors,
   load to `geo_units` with `ST_Multi(ST_Transform(...,4326))`. Pull the current OMB/Census
   delineation CSV to verify CBSA codes and county membership (expect 38060 and 27220; fail loudly
   if not). Idempotent upserts keyed on `geo_key`. `argusvue geo load --region phx|jh`.
6. **Ancestor derivation.** `geo/ancestors.py`: for a key, walk parents by code where the hierarchy
   is strict (tract → county → state → country; place/zcta → county via PostGIS
   `ST_Contains`/largest-overlap; county → cbsa via delineation table; anything → sub via membership;
   anything → h3 via centroid). Unit tests with small synthetic polygons plus a DB-backed test on
   real loaded geometry.
7. **Submarkets.** Load `submarkets.yaml`, verify every member ZCTA/place exists in `geo_units`
   (fail loudly), union geometries into `submarkets.geom`.
8. **CLI.** `argusvue geo resolve <key>` prints the key, its name, and the ancestor chain
   (`country`, `state`, `cbsa`, `county`, `place` where applicable, `sub` memberships, `h3` r7–9).
9. **Docs + commit.** `docs/DEMO.md` with the exact commands, `docs/PROGRESS.md`,
   `docs/DECISIONS.md` entries for any spec deviation, one commit.

Acceptance (from SPEC §11): `geo resolve zcta:85085` → county 04013, cbsa 38060, state 04, sub
`phx-north-tsmc`; `geo resolve zcta:83014` → county 56039, cbsa 27220, state 56, sub `jh-wilson`;
all seed ZCTAs validated; tests green for key parsing and containment.

Environment notes that shape phase 0 (checked in this session): `uv` present; Python 3.12.3 and
3.13 available; Postgres 16 client and server binaries present but **no PostGIS/pgvector packages
and no Docker daemon**; outbound HTTPS to census.gov works; no `ANTHROPIC_API_KEY` or
`PARALLEL_API_KEY` in the environment.

## 3. Questions (max 5) — answers that would change the design

1. **Repository.** This session is attached to `nachobeesknees/cybertruck-weather`, a one-file
   weather dashboard unrelated to Argusvue. Should Argusvue live (a) in a new repo you create and
   attach, or (b) in this repo on a branch, replacing the dashboard at root? If no answer, I'll build
   on branch `argusvue` in this repo, leave `index.html` untouched, and note it in DECISIONS.md.

2. **Database for development and tests in this cloud session.** There is no Docker daemon here, and
   the image's Postgres 16 lacks PostGIS. Options: (a) I `apt-get install postgresql-16-postgis-3`
   and `pgvector` and run a local Postgres in the container for tests, with `docker-compose.yml`
   provided for your machine; (b) you add a reachable `DATABASE_URL` to the environment; (c) tests
   are written but only run on your machine. Default: (a).

3. **API keys.** Phases 2–4 need `ANTHROPIC_API_KEY` and `PARALLEL_API_KEY`. Will you add them as
   environment secrets for this session, or should those phases be built against recorded fixtures
   and golden files, with live runs executed on your machine? Default: fixtures here, live on your
   machine, which also changes how the "≥ 30 monitors running" criterion is demonstrated.

4. **Phase 5 "5 consecutive days".** Must this be five wall-clock days of the scheduler on your
   machine, or is five briefs produced with `--date` over five successive as-of dates acceptable as
   the acceptance demo? Default: as-of dates for the demo, with the scheduler left running on your
   machine for the real thing.

5. **Gazetteer extent.** Load only the region counties (plus their state/CBSA/country ancestors,
   and every ZCTA/place/tract that intersects them), or the whole of AZ, WY and ID? Region-only
   keeps phase 0 small (minutes, not hours) but means a claim geocoded outside the region resolves
   to county/state only. Default: region-only, with the loader able to extend to full states later.

If a question goes unanswered I'll use the stated default and record it in `docs/DECISIONS.md`.

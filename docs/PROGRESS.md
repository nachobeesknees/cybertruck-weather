# PROGRESS

| Phase | Status | Notes |
|---|---|---|
| 0 scaffold + geography | **done** 2026-11-05 | Acceptance met: `geo resolve zcta:85085` / `zcta:83014` correct; all seed ZCTAs validated; 42 tests green; idempotent load proven. |
| 1 structured ingest + indicators | not started | |
| 2 documents + collectors | not started | Own agents (Claude web search + feeds + own fetcher) replace Parallel/Exa; see DECISIONS. |
| 3 claims | not started | |
| 4 theses | not started | |
| 5 render + eval | not started | |

## Phase 0 detail

Works: `db migrate`, `geo load --region`, `geo resolve <key>` (gazetteer keys and `h3:` cells),
typed config loaders, job-run logging, migration runner, PostGIS containment functions
`geo_containing(point)` and `geo_ancestors(key)`.

Deferred to later phases: `spend` is a placeholder until the spend ledger (phase 2); parcel keys parse
but have no gazetteer rows until the assessor ingest (phase 1); H3 cells are computed, not stored.

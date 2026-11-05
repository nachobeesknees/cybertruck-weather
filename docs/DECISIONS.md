# DECISIONS

Every deviation from `docs/SPEC.md`, every unavailable source and its fallback, and every plan
question resolved by default. Newest at the bottom.

| Date | Decision | Why |
|---|---|---|
| 2026-11-05 | Python 3.12 pinned via `uv` (`.python-version`); session image defaults to 3.13. | SPEC §2 says 3.12. |
| 2026-11-05 | **Own collection agents replace Parallel and Exa as the default worker.** `collect/` will ship `own_search` (Claude server-side web search + web fetch through `llm.py`), `feed_monitor` (RSS/Atom polling + scheduled refetch with simhash diff) and `own_fetch` (allowlisted URLs, robots.txt honoured). `parallel_*` and `exa_search` stay as stubbed adapters behind the same `Collector` interface, enabled only if their key is set. | Owner preference (2026-11-05). Fewer vendors; citations and excerpt verification are already ours. Cost: bot-blocked sites will fail and get logged here as unavailable. |
| 2026-11-05 | Repo: Argusvue lives on branch `argusvue` of `nachobeesknees/cybertruck-weather`; the dashboard `index.html` is untouched. | Plan Q1 unanswered; default. |
| 2026-11-05 | Dev DB in the cloud session is a locally installed Postgres 16 + PostGIS 3.4 + pgvector 0.6 (apt). `docker-compose.yml` + `docker/postgres.Dockerfile` ship for the owner's machine (official postgis image + pgvector package). | Plan Q2 unanswered; no Docker daemon in the session. |
| 2026-11-05 | API keys: only `ANTHROPIC_API_KEY` is required now. Phases 2–4 are built against fixtures/golden files where a key is absent; live runs happen wherever the key is set. | Plan Q3 unanswered; own-agents decision collapses the key list. |
| 2026-11-05 | Phase 5 "5 consecutive days" will be demonstrated with `brief --date` over five successive as-of dates; the scheduler is left running on the owner's machine for real days. | Plan Q4 unanswered; default. |
| 2026-11-05 | Gazetteer extent: states, CBSAs and counties are loaded nationally (cheap: 56/935/3235 rows) so out-of-region claims resolve coarsely; places, ZCTAs and tracts are loaded only for the region (representative point inside the region's counties, plus anything named in config). | Plan Q5 unanswered; default, slightly widened for the coarse levels. |
| 2026-11-05 | Vintages: TIGER/Line 2024; OMB July 2023 delineations (`list1_2023.xlsx`). Verified from the file: CBSA 38060 = Maricopa + Pinal; CBSA 27220 = Teton WY (central) + Teton ID (outlying). | SPEC §3 asked to verify codes rather than assume. |
| 2026-11-05 | `country:US` has no geometry and is the universal ancestor in `geo_containing()`. | Avoids a slow national union; nothing resolves outside the US. |
| 2026-11-05 | Ancestors are derived by containment of a unit's `ST_PointOnSurface` in coarser units (enum order = hierarchy order). Submarkets additionally count as ancestors of their explicit members (so a `place` that is a submarket member lists it even though `sub` ranks finer than `place`). `parent_key` is structural where the code defines it (tract/parcel→county, state→country), delineation-based for county→CBSA (else state), containment otherwise. | SPEC §3 "derived via PostGIS containment"; the explicit-membership rule keeps place pages consistent with submarket theses. |
| 2026-11-05 | Submarkets are stored both in `submarkets` (member list) and as `geo_units` rows at level `sub`, so every key type resolves through one table. | Uniform ancestor derivation. |
| 2026-11-05 | Jackson places: Moose and Moran are not Census places; they are covered by ZCTAs 83012/83013 in `sub:jh-north`. Moose Wilson Road CDP (5654402), Kelly CDP, Hoback CDP, Rafter J Ranch CDP, South Park CDP and the Star Valley towns are loaded instead. | TIGER has no such units. |
| 2026-11-05 | TIGER NAD83 (EPSG:4269) geometries are transformed to 4326 on insert and run through `ST_MakeValid`. | SPEC DDL says 4326. |
| 2026-11-05 | `submarkets.yaml` members may be `zcta:` or `place:` keys (east valley, Scottsdale and west valley use places; the rest use ZCTAs). | SPEC §3 allows "ZCTAs (or places)". |

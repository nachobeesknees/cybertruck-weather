# DEMO

Exact commands to demonstrate each completed phase. Run from the repo root.

## Prerequisites

```bash
uv sync                                   # Python 3.12 env + deps
cp .env.example .env                      # defaults point at localhost:5432
docker compose up -d --build db           # Postgres 16 + PostGIS 3.4 + pgvector (your machine)
uv run argusvue db migrate
```

In a cloud session without Docker, the equivalent is a local install:
`apt-get install postgresql-16-postgis-3 postgresql-16-pgvector`, `service postgresql start`,
then create role `argusvue` and databases `argusvue` and `argusvue_test`.

## Phase 0 — scaffold + geography

```bash
uv run argusvue geo load --region phx     # ~30 s after first download (~650 MB of TIGER files, cached)
uv run argusvue geo load --region jh

uv run argusvue geo resolve zcta:85085    # -> state:04, cbsa:38060, county:04013, place:0455000, sub:phx-north-tsmc
uv run argusvue geo resolve zcta:83014    # -> state:56, cbsa:27220, county:56039, sub:jh-wilson
uv run argusvue geo resolve h3:8926b61805bffff   # any point: resolve via H3 cell centre
uv run argusvue geo resolve county:56023  # commuter shed: region jh, no CBSA ancestor

# Idempotency: the second load adds zero rows.
psql "$DATABASE_URL" -c "select count(*) from geo_units"
uv run argusvue geo load --region phx
psql "$DATABASE_URL" -c "select count(*) from geo_units"

# Every job is recorded.
psql "$DATABASE_URL" -c "select job, region_id, ok, finished_at - started_at from job_runs"

# Tests: pure (keys), synthetic PostGIS gazetteer (containment rules), real gazetteer (acceptance).
uv run pytest -q                           # 42 passed
uv run pytest -q -m gazetteer              # the §11 acceptance checks on real data
```

Expected `geo load` stats: phx 56 states, 935 CBSAs, 3235 counties (national, coarse coverage), 84
places, 162 ZCTAs, 1104 tracts, 6 submarkets; jh 36 places, 26 ZCTAs, 19 tracts, 6 submarkets.

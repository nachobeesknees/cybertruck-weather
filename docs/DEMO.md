# DEMO

Exact commands to demonstrate each phase. Run from the repo root (`export UV_SYSTEM_CERTS=1` first in the cloud
session). Everything below was executed on 2026-11-05 against the local Postgres; outputs quoted are from that run.

## Prerequisites

```bash
uv sync                                   # Python 3.12 env + deps
cp .env.example .env                      # DATABASE_URL etc.; add ANTHROPIC_API_KEY for live LLM steps
docker compose up -d --build db           # Postgres 16 + PostGIS + pgvector (your machine)
uv run argusvue db migrate                # 7 migrations
uv run pytest -q                          # 212 tests (geo, ingest, dedup, reconcile, llm spend guard, validator, render, eval)
```

## Phase 0 — geography

```bash
uv run argusvue geo load --region phx && uv run argusvue geo load --region jh     # idempotent; ~30 s each after download
uv run argusvue geo resolve zcta:85085    # -> state:04, cbsa:38060, county:04013, place:0455000, sub:phx-north-tsmc
uv run argusvue geo resolve zcta:83014    # -> state:56, cbsa:27220, county:56039, sub:jh-wilson
```

## Phase 1 — structured ingest + indicators

```bash
uv run argusvue sources list
uv run argusvue ingest all --region phx --backfill      # zillow, redfin, apartment_list, irs_soi, fred, bls_qcew, census_pep, census_bps
uv run argusvue ingest all --region jh --backfill       # + snotel, nps_stats, wy_revenue (census_acs needs CENSUS_API_KEY)
uv run argusvue ingest freddie_mac --region '*' --backfill
uv run argusvue ingest zillow --region phx              # re-run: every document skipped, inserted = 0
uv run argusvue indicators list --region phx            # 54 indicators configured; >40 with data
uv run argusvue indicators show median_sale_price --geo cbsa:38060 --last 12
uv run argusvue indicators show sales_use_tax_distribution --geo county:56039 --last 14
uv run argusvue indicators show pop_estimate --geo county:04013 --last 20 --revisions   # vintage revisions kept
```
Coverage (2026-11-05): Phoenix 2000-2026 ZHVI for 149 ZIPs, Redfin metro/county 2012-, FRED jobs/unemployment 1990-,
QCEW 2014-, permits 2010- for every city and county, migration 2012-2023, population estimates 2010-2025; Jackson adds
sales/use tax 2003-, lodging tax 2011-, Grand Teton visits 1979-, four SNOTEL sites 1980-, airport (blocked, see DECISIONS).

## Phase 2 — documents + collectors

```bash
uv run argusvue collect run --region phx --all --max-items 8   # 53 monitors: 45 feed/page/json/url ran, 8 search need the API key
uv run argusvue collect run --region jh --all --max-items 8    # 33 monitors
uv run argusvue collect status --region phx                    # attention ledger: last check, result, next due
uv run argusvue eval dedup                                     # raw -> unique -> near-dups -> extracted -> claims
uv run pytest -q tests/test_llm.py -k spend                    # spend guard: cap reached => call refused before the API
uv run argusvue spend
```

## Phase 3 — claims

```bash
uv run argusvue extract run --region phx --limit 20     # needs ANTHROPIC_API_KEY; verbatim excerpts enforced
uv run argusvue reconcile run --region phx              # corroborate / merge / contest / supersede
uv run argusvue conflicts list --region phx
uv run argusvue conflicts resolve <id> --keep a --note "..."
uv run argusvue eval precision --sample 100 --region phx   # out/audits/<date>-phx.csv for labelling
uv run pytest -q tests/test_reconcile.py tests/test_extract.py   # the four reconcile cases on fixtures; excerpt rejection
```

## Phase 4 — theses

```bash
uv run argusvue theses seed                  # 6 phx + 5 jh seeds, falsifiers resolved against live indicator values
uv run argusvue theses list --region phx
uv run argusvue theses show 1
uv run argusvue quant run --region phx       # anomalies (z vs 36 months, YoY), falsifier checks, lead-lag on causal edges
uv run argusvue quant anomalies --region phx --limit 15
uv run argusvue theses review-due --region phx --list
uv run argusvue theses review 1 --force      # Triage -> Analyst -> Validator -> Critic -> Gate (needs ANTHROPIC_API_KEY)
uv run argusvue theses events 1              # full log with agent_run ids
uv run argusvue theses approve 1 --note "ok" # human gate for status changes
uv run pytest -q tests/test_review_loop.py   # validator rejects out-of-allowlist ids; critic approve/revise/reject fixtures
```

## Phase 5 — render + eval

```bash
for d in 2026-11-01 2026-11-02 2026-11-03 2026-11-04 2026-11-05; do
  uv run argusvue brief --region phx --date $d; uv run argusvue brief --region jh --date $d; done   # out/briefs/
uv run argusvue places render --region phx   # out/places/<geo_key>.md (23 pages per region)
uv run argusvue eval cost --days 7
uv run argusvue eval staleness --region phx
uv run argusvue eval recall --region jh
uv run argusvue eval calibration
uv run argusvue replay --as-of 2022-06-30 --thesis 5     # dry-run loop on evidence observed by then (needs ANTHROPIC_API_KEY)
uv run argusvue scheduler start               # hourly due-monitor sweep, daily/weekly ingests (America/Phoenix)
```

## Session mode — running the loop without an API key

Set `LLM_PROVIDER=session` in `.env`. Every model call then becomes a request file instead of an HTTP call: the
job writes `out/llm/inbox/<id>.json` (role, system prompt, packet, JSON schema) and raises "pending model answer"
until `out/llm/outbox/<id>.json` exists with `{"answered_by": "<who>", "answer": {...}}`. Answers are validated,
logged to `agent_runs` (model `session:<who>`, cost 0) and used exactly like API answers. The Claude Code session
and its subagents answer the files (sonnet for extraction/entailment, opus for analyst/critic, the overseeing
session for the editor), then the same commands are re-run.

```bash
uv run argusvue llm status                    # requests / answered / pending per role
uv run argusvue llm pending --role critic     # what is waiting, with packet sizes
uv run argusvue extract run --region phx      # first run writes requests, second run ingests the answers
for id in 1 2 3 4 5 6 7 8 9 10 11; do uv run argusvue theses review $id --force; done
uv run argusvue llm carry-forward --role analyst   # re-use an answer when its packet changed only cosmetically
uv run argusvue intel --region phx            # editor request -> answer -> out/intel/<date>-phx.md
```

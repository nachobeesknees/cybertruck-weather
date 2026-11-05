# Argusvue — build brief for Claude Code

You are building the pilot of **Argusvue**: a "living synthesis layer" that keeps a continuously updated, source-traceable model of what is happening in a place's real estate market and regional economy, and can explain its reasoning with citations.

Pilot geographies: **metro Phoenix, AZ** and **Jackson, WY** (Teton County WY plus its commuter shed). They are deliberately different in scale (~5M people vs ~25k) and in data density (Phoenix has rich open feeds; Jackson's signal lives in PDF agendas, local news and a handful of state datasets). The geography and source abstractions must work for both without special-casing.

Read this whole file before doing anything, then follow **§12 How to work**.

---

## 1. Scope

**Build:** a Python backend that

1. ingests structured feeds and open-web documents on a schedule,
2. extracts typed, cited claims from documents,
3. reconciles claims into an append-only world model (entity graph, claim ledger, indicator series),
4. maintains versioned theses per geography through a Triage → Analyst → Validator → Critic loop with a human approval gate,
5. renders a daily Markdown brief per region and a Markdown page per place, each built only from claim/indicator ids.

**Do not build in the pilot:** a web UI, auth, multi-tenancy, licensed-data ingestion (ARMLS, CoStar, Teton Board of Realtors MLS), vector-database-as-memory, or a workflow engine (Temporal/Prefect/Airflow). CLI + Markdown output only.

**Non-negotiable principles**

- Nothing is overwritten. Claims and thesis versions are immutable; "current belief" is a view.
- Every rendered sentence that states a fact carries claim or indicator ids. The renderer refuses sentences that don't.
- Geography is a parameter of every job, never an agent's identity. Same code runs both regions from config.
- LLMs propose, code validates, statistics grade.
- Bi-temporal ledger (valid time vs observed time) so "what did we believe on date X" is answerable for backtests.
- Respect robots.txt and site terms. Never scrape paywalled or licensed content. Cite public brokerage/market reports; don't republish their tables.

---

## 2. Stack (decided)

- Python 3.12, `uv`, `pydantic` v2, `typer` CLI, `httpx`, `trafilatura` (HTML → text), `pdfplumber` (PDF → text), `h3`, `shapely`, `psycopg` (v3).
- Postgres 16 + PostGIS + pgvector via `docker compose`. Plain numbered SQL migrations in `migrations/`, applied by a small runner. No heavy ORM; parameterised SQL in a thin repository layer is fine.
- Scheduler: APScheduler in-process. Every job is an idempotent function; re-running never duplicates rows. Record every run in `job_runs`.
- Open-web collection: Parallel (Task API, Monitor API, Extract API) as the default worker; Exa Search as an optional adapter behind the same `Collector` interface. Own fetcher only for an explicit URL allowlist (agendas, data portals, CSV downloads).
- LLM: Anthropic API through one module `argusvue/llm.py`. Model per role lives in `config/models.yaml`. Defaults: triage = `claude-haiku-5-5`, extraction/resolution = `claude-sonnet-5-5`, analyst/critic = `claude-opus-5-5`. Verify current model IDs at docs.claude.com before pinning. Every call is logged to `agent_runs` with role, model, prompt hash, input/output tokens, cost, latency.
- Blob storage: `data/snapshots/` on local disk behind a `BlobStore` class with an S3-compatible interface (so it can move later).
- Secrets in `.env`: `ANTHROPIC_API_KEY`, `PARALLEL_API_KEY`, `EXA_API_KEY` (optional), `DATABASE_URL`, `DAILY_SPEND_CAP_USD` (default 20). Never commit `.env`.
- Spend guard: `llm.py` and the collector adapters check today's spend before each call; when the cap is hit, jobs stop cleanly and the brief reports it.

Repo layout (create this):

```
argusvue/
  cli.py            # typer entrypoints (see §11)
  config/           # regions.yaml, submarkets.yaml, sources.yaml, indicators.yaml, models.yaml, theses_seed.yaml
  geo/              # gazetteer load, geo_key parsing, ancestor derivation, h3
  ingest/           # one module per structured source
  collect/          # Collector interface + adapters: parallel_task, parallel_monitor, parallel_extract, exa_search, own_fetch
  store/            # db helpers, repositories, BlobStore
  extract/          # document → claims
  resolve/          # entity resolution, geocoding to geo_key
  reconcile/        # corroborate / supersede / contest / decay / conflicts
  quant/            # indicator anomalies, lead-lag tests, falsifier checks
  agents/           # triage, analyst, validator, critic, librarian, editor (prompts in prompts/*.md)
  render/           # briefs and place pages
  eval/             # recall, precision audit, staleness, cost, calibration, replay
  scheduler.py
migrations/
tests/
docs/               # SPEC.md (this file), PLAN.md, DECISIONS.md, DEMO.md, PROGRESS.md
out/                # briefs/, places/, audits/
data/               # snapshots/, downloads/ (gitignored)
```

---

## 3. Geography model

### Levels and keys

`geo_key` is a string `{level}:{code}`; levels, broad to fine:

`country:US` → `state:04` → `cbsa:38060` → `county:04013` → `place:0455000` → `sub:phx-north-tsmc` (custom) → `zcta:85085` → `tract:04013101001` → `parcel:04013:<APN>`; plus `h3:<cell>` at resolution 7–9 for aggregation that ignores admin lines.

Load gazetteer geometry from Census TIGER/Line (states, CBSAs, counties, places, ZCTAs, tracts) and CBSA composition from the current OMB/Census delineation file. Never hardcode names; codes only. Store ancestors on write: every claim gets a `claim_geo` row per ancestor level, derived via PostGIS containment from its most specific key.

### Regions (config/regions.yaml)

A region is a named set of counties plus a CBSA; it may include counties outside the CBSA (commuter shed).

**phx** — CBSA Phoenix-Mesa-Chandler, AZ (expected code 38060; verify from the delineation file). Counties: Maricopa, Pinal. Places of interest: Phoenix, Mesa, Chandler, Gilbert, Scottsdale, Tempe, Glendale, Peoria, Surprise, Goodyear, Buckeye, Queen Creek, Casa Grande.

**jh** — CBSA Jackson, WY-ID micropolitan (expected code 27220; verify). Counties: Teton WY, Teton ID. Extended commuter shed: Lincoln County WY (Alpine, Star Valley). Places of interest: Town of Jackson; unincorporated Wilson, Teton Village, Moose, Moran, Kelly, Hoback; Alpine; Victor, Driggs, Tetonia (ID).

### Submarkets (config/submarkets.yaml)

Submarkets are named unions of ZCTAs (or places). Seed:

- phx: `phx-central` (Phoenix core ZIPs), `phx-north-tsmc` (85085, 85083, 85027, 85310 and neighbours — verify against the TSMC campus location), `phx-east-valley` (Tempe, Chandler, Gilbert, Mesa), `phx-scottsdale`, `phx-west-valley` (Goodyear, Buckeye, Surprise, Avondale), `phx-pinal` (Queen Creek/San Tan, Casa Grande, Maricopa).
- jh: `jh-town` (83001), `jh-wilson` (83014), `jh-teton-village` (83025), `jh-north` (83012, 83013, 83011), `jh-alpine` (83128, Lincoln Co.), `jh-teton-valley-id` (83455, 83422, 83452).

Verify every ZCTA exists in the gazetteer at load time and fail loudly if not.

---

## 4. Data model (DDL sketch — refine, don't dilute)

```sql
regions(id text pk, name, cbsa_code, county_fips text[], config jsonb)
geo_units(geo_key text pk, level, code, name, parent_key, region_id, geom geometry(MultiPolygon,4326))
submarkets(geo_key text pk, region_id, name, member_keys text[], geom)

sources(id serial pk, name, kind enum('structured','web_monitor','web_task','own_fetch','pdf'),
        region_id, url, cadence interval, reliability numeric default 0.5, license_note, active bool)

documents(id bigserial pk, source_id, url, fetched_at timestamptz, content_hash bytea, simhash bigint,
          title, published_at, blob_ref, text_len int, origin_key text)   -- origin_key: canonical origin (wire story, press release) for independence counting
UNIQUE(content_hash)

entities(id bigserial pk, type enum('place','parcel','project','org','person','instrument','event'),
         canonical_name, region_id, geo_key, geom, created_at, merged_into bigint null)
entity_ids(entity_id, scheme text, value text, PRIMARY KEY(scheme,value))   -- APN, FIPS, CBSA, ZCTA, CIK, EIN, OSM, WY/AZ SOS filing no.
entity_links(src_entity, predicate, dst_entity, claim_id)

claims(id bigserial pk, subject_entity bigint, predicate text, object_entity bigint null,
       value_num numeric null, value_text text null, unit text null,
       geo_key text, valid_from date null, valid_to date null, observed_at timestamptz,
       document_id bigint, excerpt_start int, excerpt_end int, excerpt text,
       extractor_version text, source_reliability numeric, extraction_conf numeric,
       corroboration_count int default 1, confidence numeric,
       status enum('asserted','corroborated','contested','superseded','retracted'),
       half_life_days int, superseded_by bigint null, created_at)
claim_geo(claim_id, geo_key, level)      -- one row per ancestor level
claim_origins(claim_id, origin_key)      -- distinct origins supporting the claim

indicators(id text pk, name, unit, direction_good text, half_life_days, source_id, region_scope text[])
indicator_points(indicator_id, geo_key, period date, value numeric, claim_id bigint, observed_at,
                 PRIMARY KEY(indicator_id, geo_key, period, observed_at))   -- keep revisions

conflicts(id, claim_a, claim_b, kind, opened_at, resolved_at, resolution, resolver_run_id)

theses(id bigserial pk, region_id, geo_key, domain, created_at, current_version int, human_status enum('pending','approved','rejected'))
thesis_versions(thesis_id, version, statement, status enum('emerging','established','weakening','dead'),
                confidence numeric, confidence_reasoning text, implications jsonb, change_summary text,
                analyst_run_id, critic_run_id, critic_verdict, known_disputes jsonb, created_at,
                PRIMARY KEY(thesis_id, version))
thesis_claims(thesis_id, version, claim_id, role enum('support','counter'))
causal_edges(id, thesis_id, version, cause, effect, mechanism, lag_months int, strength text,
             evidence_claim_ids bigint[], status enum('hypothesis','tested'), test_result jsonb)
falsifiers(id, thesis_id, version, indicator_id, geo_key, operator, threshold numeric, by_date date, action text)
falsifier_checks(falsifier_id, checked_at, value, triggered bool)

agent_runs(id, role, model, prompt_hash, region_id, geo_key, input_ref, output_ref, input_tokens, output_tokens, cost_usd, latency_ms, ok bool, error, created_at)
job_runs(id, job, region_id, started_at, finished_at, ok, stats jsonb, error)
attention_ledger(source_id, target text, last_checked_at, last_result text, next_due_at)
open_questions(id, region_id, geo_key, question, suggested_collector_task jsonb, raised_by_run bigint, status)
review_decisions(id, thesis_id, version, decision, note, decided_at)   -- human gate
```

---

## 5. Sources

For every structured source: write an `ingest/<source>.py` with `backfill()` and `incremental()`, map rows to `indicator_points` with a synthetic claim per point (document = the download, excerpt = the row), and record the license note. If a listed source turns out not to be downloadable, fall back to a Parallel monitor/task and mark the indicator `low_confidence`; note it in `docs/DECISIONS.md`. Check availability before assuming.

### Phoenix — structured (free)

- Redfin Data Center TSVs (metro/county/city/zip; monthly and weekly): median sale price, new listings, inventory, days on market, months of supply, price drops, sale-to-list.
- Zillow Research CSVs: ZHVI, ZORI, inventory, share of listings with price cut, days to pending.
- Apartment List rent estimates (metro/city).
- BLS CES and LAUS for the Phoenix MSA; Arizona Office of Economic Opportunity releases.
- Census ACS 5-year (population, households, median income by tract/ZCTA).
- MAG (Maricopa Association of Governments) regional datasets: building permits, population estimates.
- City of Phoenix open data: building permits. Mesa/Chandler/Gilbert/Scottsdale permit portals where downloadable.
- Maricopa County Assessor bulk parcel data (APN, use code, year built, last sale).
- Maricopa County Recorder: notices of trustee sale (counts by month; if no free feed, Parallel monthly task on public reporting, low confidence).
- ADWR Assured Water Supply determinations and groundwater announcements (PDF/HTML).
- Freddie Mac PMMS (national mortgage rate, context only). IRS SOI county-to-county migration (annual).

### Phoenix — open-web monitors (Parallel Monitor, daily unless noted)

Housing market news (AZCentral, Phoenix Business Journal, Axios Phoenix); major employer announcements (TSMC and suppliers, Intel, Amkor, data-center operators; hourly event stream for the top 5); APS/SRP large-load and transmission news; ADWR/CAP water policy; city council and planning-commission agendas for the places above (weekly, PDF via Parallel Extract); Arizona Corporation Commission filings for named developers; Cromford Report public posts (benchmark only — never ingest subscriber content).

### Jackson — structured (free)

- Wyoming Department of Revenue: monthly sales & use tax and lodging tax collections, Teton County and Lincoln County.
- Wyoming DWS QCEW: Teton County employment and wages by industry (quarterly).
- Jackson Hole Airport monthly enplanements (check publication format).
- NPS visitation statistics: Grand Teton NP monthly recreation visits.
- Teton County WY GIS/Assessor: parcels (open), sales where published.
- Town of Jackson and Teton County building permit reports; Teton County ID permits.
- Teton County Housing Department: deed-restricted inventory, waitlists, lottery results.
- Census ACS 5-year for Teton WY, Teton ID, Lincoln WY.
- SNOTEL (Jackson-area sites) snow-water equivalent — tourism season proxy.
- WYDOT: Teton Pass (WY-22) status and closures.

### Jackson — open-web monitors (weekly default; local news daily)

Jackson Hole News&Guide, Buckrail; Town Council, County Commission and Planning Commission agendas/minutes (PDF); Housing Department news; Travel & Tourism Board; public quarterly brokerage market reports (cite only); Wyoming Legislature bills on property tax, STRs, housing; Teton Valley News (ID); Teton Pass/WYDOT alerts (daily).

---

## 6. Pipeline rules

**Collect.** Each `Collector` adapter implements `run(objective, schema, geo_scope, budget) -> list[RawDocument]`. Snapshot every fetched page/PDF to `BlobStore` before anything else. Dedup by `content_hash` (exact) and simhash Hamming distance ≤ 3 (near-dup). Assign `origin_key` (wire service, press release, agency) so syndicated copies count once. Update `attention_ledger` on every check, including "nothing new".

**Extract.** Per-domain claim schemas in `extract/schemas/` (housing, employment, policy, infrastructure, water, tourism). Extractor returns claims with excerpt offsets; reject any claim whose excerpt isn't found verbatim in the document text. Parallel Task "basis" (citations, excerpts, confidence) maps directly onto claim fields — use it when the document came from a Task run.

**Resolve.** Entity resolution: blocking on normalised name + geo + type, scored candidates, LLM adjudication only for ties; new entities created as `candidate` until a second source references them. Geocode: parcel → APN; address → ZCTA/tract via Census geocoder; named places via gazetteer.

**Reconcile** (new claim vs existing claims with same subject + predicate and overlapping valid time):
- agree → corroborate: `corroboration_count += 1` only for a new `origin_key`; confidence rises with diminishing returns (`conf = 1 - Π(1 - r_i)` over origin reliabilities, capped 0.95).
- numeric differs within tolerance (per predicate) → merge, keep both excerpts.
- contradict → both `contested`, open a conflict; Resolver may schedule a targeted Parallel Task to adjudicate; unresolved after 2 attempts → stays contested and surfaces in the brief.
- newer `valid_from` → old claim `superseded`, `valid_to` set, `superseded_by` linked.

**Decay.** `effective_confidence = confidence * 0.5 ** (age_days / half_life_days)`; half-life by predicate class (listing price 14d, listing status 30d, construction status 120d, employment figure 90d, zoning/policy 730d, ownership 365d). Librarian re-verifies claims that are load-bearing in any approved thesis when effective confidence < 0.4 (cheap targeted Task run).

---

## 7. Indicators and seed theses

**Indicators (config/indicators.yaml).** Phoenix: median sale price, DOM, active inventory, months of supply, price-cut share, new listings, ZORI rent, Apartment List rent, permits (SF units, MF units) by city, nonfarm jobs by sector, unemployment rate, trustee-sale notices, ADWR determinations count, net migration (annual). Jackson: Teton County sales tax and lodging tax collections, airport enplanements, GTNP visits, QCEW employment and average wage, building permits (town, county, Teton ID), deed-restricted units and waitlist length, SNOTEL SWE, Teton Pass closure days, plus Redfin/Zillow series where ZIP coverage exists. Backfill ≥10 years wherever the source allows.

**Seed theses (config/theses_seed.yaml).** All start `emerging`, confidence ≤ 0.4, `human_status = pending`, each with ≥1 machine-checkable falsifier.

Phoenix: (1) North Phoenix/TSMC corridor demand pulling forward prices and rents; (2) data-center buildout colliding with power capacity and local political resistance; (3) groundwater/assured-water-supply limits constraining lot supply in the West Valley and Pinal; (4) apartment oversupply and concessions suppressing rent growth; (5) rising price-cut share and DOM as a 3–6 month lead on price; (6) slowing net in-migration weakening absorption.

Jackson: (1) supply fixed by federal land ownership, so price is set by wealth migration, not local income; (2) workforce housing shortage pushing the labour shed over Teton Pass and to Alpine, with pass closures as the acute risk; (3) STR regulation and lodging-tax trends as the lead on local-services demand; (4) airport enplanements and GTNP visitation as leads on lodging tax; (5) Wyoming property-tax relief legislation and its effect on Teton County revenue and assessed values.

---

## 8. Agents

All agents are stateless functions parametrised by `(region_id, geo_key, domain)`; state lives only in Postgres. Prompts live in `agents/prompts/*.md` and are versioned by hash.

**Scout** — runs collectors on schedule; also pulls `open_questions` and turns them into targeted Task runs.
**Extractor / Resolver** — as in §6.
**Quant** — nightly: recompute indicator anomalies (z-score vs trailing 36 months, plus YoY), run `falsifier_checks`, run lead-lag correlation for each `causal_edge` with ≥ 36 overlapping monthly points and store `test_result`.
**Librarian** — decay, re-verification scheduling, simhash maintenance.
**Editor** — renders (§9).

### Thesis review loop

Triggers: indicator anomaly on a thesis's geo/domain; new or superseded claim linked to a thesis entity; falsifier triggered; weekly scheduled review per thesis.

1. **Triage** (haiku). Input: thesis current version + delta (new/superseded claims, resolved conflicts, indicator moves, falsifier results). Output: `{material: bool, reason}`. Not material → log and stop.
2. **Analyst** (opus). Input packet built by code: current version, delta, parent and sibling theses, relevant leading-indicator entries with expected lags, and an **allowlist** of claim and indicator ids. Output (pydantic, strict):

```json
{"action":"no_change|update|split|retire|propose_new",
 "statement":"...", "status":"emerging|established|weakening|dead",
 "confidence":0.0, "confidence_reasoning":{"independence":"","recency":"","corroboration":"","mechanism":"","base_rate":""},
 "support":[claim_ids], "counter":[claim_ids],
 "causal_edges":[{"cause":"","effect":"","mechanism":"","lag_months":0,"strength":"weak|moderate|strong","evidence":[ids],"status":"hypothesis"}],
 "implications":[{"text":"","cites":[ids],"horizon_months":0}],
 "falsifiers":[{"indicator_id":"","geo_key":"","operator":"<|>|<=|>=","threshold":0,"by_date":"YYYY-MM-DD","action":"downgrade|retire|review"}],
 "open_questions":[{"question":"","collector_task":{...}}],
 "change_summary":""}
```
Prompt rules: cite only allowlisted ids; every implication cites ≥1 id or is tagged `hypothesis`; confidence > 0.6 requires ≥ 2 distinct origins in support; at least one new falsifier per `update`; `change_summary` states what is new versus the base version and why it matters — restating the thesis fails.
3. **Validator** (code, no LLM). Rejects and re-prompts once with the error list: ids outside allowlist; entities not in graph; falsifiers that don't resolve to real indicator/geo; `update` with no new support claims; cited claims past half-life flagged. Entailment check per implication using the extraction model: claim excerpt + sentence → entailed/neutral/contradicted; contradicted = reject, neutral = flag for critic.
4. **Critic** (opus, separate prompt, sees the proposal and packet but not the analyst's reasoning). Fixed checklist, structured output: rival mechanisms per causal edge; base-rate check against reference class; source audit (independence, incentives, specificity); missing evidence the mechanism predicts; overreach ("will" vs "may", numbers without data); consistency with parent/sibling theses; verdict `approve|revise|reject` with specific edits and requested collector tasks.
5. **Gate.** approve → write version. revise → one more analyst round (max 2 total); still unresolved → write version with confidence capped at 0.5 and objections stored in `known_disputes`. reject → no version; push `open_questions`. Any **status change** additionally requires `human_status = approved` via `argusvue theses approve <id>` before it renders as current. Log every verdict.

---

## 9. Rendering

`out/briefs/YYYY-MM-DD-<region>.md` sections: what changed since yesterday (thesis diffs, new conflicts, triggered falsifiers); theses by status with confidence and one-line change summaries; indicator table (latest, MoM, YoY, z-score); open conflicts; open questions; cost today. `out/places/<geo_key>.md` per region, submarket and place: current theses, indicator snapshot, recent events. Each factual sentence ends with `[c:123]` / `[i:median_price:2026-09]` tags; the renderer drops any sentence without one and logs it.

---

## 10. Evaluation harness (`argusvue eval …`)

- `recall`: `eval/events.yaml` is a hand-curated list of notable events per region (I will maintain it); report % captured and median latency.
- `precision`: sample 100 claims/week into `out/audits/<date>.csv` for human labelling; compute precision from labelled files.
- `dedup`: raw docs → unique docs → claims; re-extraction rate.
- `staleness`: median age and % past half-life of claims behind approved theses.
- `conflicts`: opened/resolved/time-to-resolve.
- `calibration`: falsifier outcomes log; Brier score when enough resolve.
- `cost`: per region-day, by role and adapter.
- `replay --as-of DATE --thesis ID`: rebuild the analyst packet using only claims with `observed_at <= DATE` and run the loop in dry-run mode; used to test whether the 2022 Phoenix turn would have been flagged.

---

## 11. Phases and acceptance criteria

**Phase 0 — scaffold + geography.** Repo, compose, migrations, config, gazetteer load for both regions, submarkets, `geo resolve`. Accept: `argusvue geo resolve zcta:85085` and `zcta:83014` return correct ancestors; all seed ZCTAs validated; tests for key parsing and containment.

**Phase 1 — structured ingest + indicators.** Accept: ≥ 8 Phoenix and ≥ 5 Jackson indicators with ≥ 10 years where available, each point traceable to a document; `indicators show` works; idempotent re-run adds zero rows.

**Phase 2 — documents + collectors.** Parallel adapters, own fetcher, snapshots, dedup, attention ledger, scheduler. Accept: ≥ 30 Phoenix and ≥ 15 Jackson monitors running; dedup stats reported; spend guard proven by test.

**Phase 3 — claims.** Extraction, resolution, reconcile, decay, conflicts, precision sampler. Accept: 100-claim audit file produced; reconcile rules unit-tested on fixtures covering all four cases; conflict CLI works.

**Phase 4 — theses.** Seeds, Quant, Triage → Analyst → Validator → Critic → gate, human approval CLI, run logging. Accept: all seed theses reviewed at least once with full logs; validator rejects a fixture with out-of-allowlist ids; critic fixtures produce all three verdicts.

**Phase 5 — render + eval.** Briefs, place pages, eval commands, replay. Accept: daily brief generated for both regions for 5 consecutive days; `eval cost` and `eval staleness` run; one replay executed.

Each phase ends with: tests green, `docs/DEMO.md` updated with the exact commands to demonstrate it, `docs/PROGRESS.md` updated, one git commit.

### CLI (typer)

`db migrate` · `geo load --region` · `geo resolve <key>` · `ingest <source> --region [--backfill]` · `collect run --region [--source]` · `extract run` · `reconcile run` · `conflicts list|resolve <id>` · `quant run --region` · `theses list|show <id>|review <id>|approve <id>|reject <id>` · `brief --region [--date]` · `places render --region` · `eval recall|precision|dedup|staleness|conflicts|calibration|cost` · `replay --as-of --thesis` · `spend` · `scheduler start`

---

## 12. How to work

1. Read this file fully. Save it as `docs/SPEC.md`. Create a short `CLAUDE.md` at repo root containing §1 principles, §2 stack, and these working rules.
2. Before writing code, write `docs/PLAN.md`: your understanding, the phase-0 task list, and **at most 5 questions** whose answers would change the design. Stop and wait for my answers. If I don't answer a question, use the defaults in §13 and record the choice in `docs/DECISIONS.md`.
3. Work one phase at a time. Don't start the next phase until the acceptance criteria pass and I've seen `DEMO.md`.
4. Tests before features for anything with rules: geo derivation, dedup, reconcile cases, decay math, validator, renderer tag enforcement, spend guard. Golden-file tests for prompts (fixture packet → expected structured output shape).
5. Every LLM output is parsed into a pydantic model; on failure, re-prompt once with the validation errors, then fail the job loudly. No silent fallbacks.
6. Never scrape paywalled or licensed sources; honour robots.txt; set a descriptive User-Agent with a contact URL, not an email.
7. Keep `docs/DECISIONS.md` current: every deviation from this spec, every unavailable source and its fallback.
8. Budget: stay under `DAILY_SPEND_CAP_USD`; prefer haiku/sonnet wherever the spec allows; batch Parallel runs.
9. Commit after each phase with a message summarising what works and what's deferred.

## 13. Defaults for questions you'd otherwise ask

- Brief delivery: write to `out/briefs/`; no email in the pilot.
- Hosting: local docker compose on my machine; nothing deployed.
- Exa: implement the adapter interface, stub the client; enable only if `EXA_API_KEY` is set.
- Parallel processors: `core` for routine Task runs, `pro` for conflict adjudication and re-verification; Monitor `lite` for weekly, `base` for daily/hourly.
- Cadence: Phoenix news daily, top-employer event streams hourly, agendas weekly; Jackson local news daily, everything else weekly.
- Entity "person" type: public officials and named executives only, by role; no private individuals.
- Languages: English only.
- Timezone for jobs and briefs: America/Phoenix.
- If a free structured source is missing, don't block the phase — fall back per §5 and move on.

# PROGRESS

| Phase | Status | Notes (2026-11-05) |
|---|---|---|
| 0 scaffold + geography | **done** | Acceptance met; 5,670 gazetteer units, 12 submarkets, ancestry materialised. |
| 1 structured ingest + indicators | **done** | 14 sources, 63 indicators configured; Phoenix 40+ and Jackson 25+ series loaded with >= 10 years where the source allows; every point is a claim citing its download row; re-runs insert 0. ACS blocked (Census API key), JH airport blocked (Cloudflare). |
| 2 documents + collectors | **done (search monitors pending key)** | 53 phx + 33 jh monitors; first sweep stored 194 web documents, 7 near-dups detected; attention ledger live; spend guard unit-tested. 13 search monitors wait for `ANTHROPIC_API_KEY`. |
| 3 claims | **built, live run pending key** | Extraction with verbatim-excerpt enforcement, entity resolution + geocoding, reconcile (4 cases unit-tested on fixtures), decay, conflicts CLI, precision sampler. No live claims yet (no API key in this session). |
| 4 theses | **built, live review pending key** | 11 seeds with resolved falsifiers; Quant ran live (anomalies, 17 falsifier checks, 3 lead-lag tests); Triage->Analyst->Validator->Critic->Gate implemented and tested with fixtures (all three critic verdicts, out-of-allowlist rejection); human approval CLI. Live reviews need the key. |
| 5 render + eval | **done** | Briefs for 2026-11-01..05 for both regions (0 dropped sentences), 46 place pages, eval recall/precision/dedup/staleness/conflicts/calibration/cost; replay implemented (dry-run loop; live run needs the key). |

## Open items
- Add `ANTHROPIC_API_KEY` to the cloud environment (or `.env` locally) to run extraction, reviews, search monitors and replay live.
- Optional `CENSUS_API_KEY` for ACS population/households/income by ZCTA.
- Jackson Hole Airport PDFs are behind a Cloudflare challenge; FAA annual enplanements are the fallback.
- Buckrail and WyoFile feeds returned 403 once during a burst and 200 on retest; monitor cadence is daily.

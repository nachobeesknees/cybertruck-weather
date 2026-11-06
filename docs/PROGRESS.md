# PROGRESS

| Phase | Status | Notes (2026-11-06) |
|---|---|---|
| 0 scaffold + geography | **done** | 5,670 gazetteer units, 12 submarkets, ancestry materialised (auto-refreshed by `db migrate`). |
| 1 structured ingest + indicators | **done** | 14 sources, 63 indicators; Phoenix 40+ and Jackson 25+ series loaded; every point is a claim citing its download row; re-runs insert 0. ACS blocked (Census API key), JH airport blocked (bot check). |
| 2 documents + collectors | **done** | 53 phx + 33 jh monitors; 202 web documents stored; near-dups detected; attention ledger live. Search monitors run in session mode (requests answered by the Claude Code session). |
| 3 claims | **done, live** | 203 documents extracted in session mode (8 sonnet agents); 240+ typed claims with verbatim excerpts, state-aware geocoding, reconcile with corroboration/supersession; 14 phx + 5 jh genuine conflicts open for review. |
| 4 theses | **done, live** | 11 seeds reviewed end to end in session mode: analyst (opus) -> validator (code + entailment) -> critic (opus) -> gate. 10 theses have reviewed second versions (confidence 0.25-0.45), thesis 8 unchanged; theses 7 and 10 carry an unresolved critic objection on record. Falsifiers support same-period tests (`period`), with a publication grace. Re-running a review is idempotent. |
| 5 render + eval | **done** | Daily briefs, 46 place pages, eval suite, replay; narrative intelligence reports (`intel`) for both regions written by the editor role with every sentence cited: `out/intel/2026-11-05-phx.md`, `out/intel/2026-11-05-jh.md`. |

## Session mode (no API key)
`LLM_PROVIDER=session`: every model call is a request file answered by the Claude Code session and its subagents
(sonnet for extraction/entailment/resolution, opus for analyst/critic, the overseeing session for the editor). 320+
requests answered this way; cost logged as 0 in `agent_runs`. `llm carry-forward` reattaches same-round answers when a
packet drifts (sibling versions, schema changes).

## Open items
- Optional `ANTHROPIC_API_KEY` to run the loop unattended (scheduler); optional `CENSUS_API_KEY` for ACS.
- Jackson Hole Airport PDFs behind a bot check (FAA enplanements fallback); Buckrail and WyoFile feeds return 403.
- Grand Teton visitation series ends December 2025; no Teton County property-tax revenue series yet.
- Review the open ARMLS conflicts (likely different months' readings) and the Jackson brokerage inventory conflicts.
- Human gate: seed theses are still `human: pending`; status changes will require `theses approve`.

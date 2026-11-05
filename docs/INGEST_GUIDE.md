# Adding a structured source

Every structured source is one module `argusvue/ingest/<slug>.py` with a `StructuredSource` subclass
(class name set by `module:` in `argusvue/config/sources.yaml`). The runner (`argusvue/ingest/runner.py`)
does everything else: snapshot to `BlobStore`, dedup by content hash, region filtering, excerpt
verification, claim + `indicator_points` upserts with revision handling, `job_runs` logging.

Read first: `argusvue/ingest/base.py` (FetchSpec, FetchedDocument, Point, IngestContext),
`argusvue/ingest/runner.py` (what is verified), your entries in `sources.yaml` and `indicators.yaml`,
`tests/helpers.py` (FakeCtx, doc_from_file) and `tests/test_ingest_runner.py` (a complete example source).

## Contract

- `fetch_specs(ctx) -> list[FetchSpec]`: the HTTP requests whose responses become documents.
  `ctx.backfill` selects full history versus the incremental window. Every URL you return must
  exist: the runner raises on 4xx/5xx. If you must discover URLs or probe availability, do a light
  `httpx` GET/HEAD inside `fetch_specs` (descriptive `argusvue.settings.USER_AGENT`, honour the
  source's `request_delay_s`). Put per-document facts (station lat/lon, fiscal year, series id) in
  `FetchSpec.meta`; `parse` gets them back as `doc.meta`. Use `FetchSpec.key` to tell files apart.
- `parse(doc, ctx) -> Iterable[Point]`: one `Point` per (indicator, geo_key, period).
  - `indicator_id`: exactly an id listed for your source in `indicators.yaml`.
  - `geo_key`: a gazetteer key from a `ctx` lookup (`zcta`, `county`, `cbsa`, `place`,
    `place_by_name`, `county_by_name`, `containing(lat, lon, level)`), or `country:US` for national
    context. If a lookup returns `None`, skip the row. Never build keys from names.
  - `period`: monthly = first day of the month; quarterly = first day of the quarter; annual = Jan 1 of
    the reference year; weekly = the published week-ending date; daily = the day. Override
    `valid_from`/`valid_to` only when the period convention is wrong for the data (ACS 5-year spans).
  - `value`: a number that appears verbatim in the excerpt row. Skip blanks, `NA`, `.`, sentinels,
    suppressed cells. A sum of columns from the *same row* is allowed only if `attrs['formula']`
    says so (e.g. `"units_2 + units_3_4 + units_5plus"`). Never combine rows or documents.
  - `excerpt`: a verbatim substring of `doc.text()` with exact `excerpt_start`/`excerpt_end`
    character offsets. For CSV/TSV/TXT use `doc.iter_lines()` (yields `(offset, line)`); for JSON
    locate the record with `doc.text().find(...)`; for PDFs `doc.text()` is the pdfplumber extraction
    with pages joined by `\f`, so use a line of it. Always use the default encoding so offsets agree
    with the runner's verification. One excerpt (one wide row) may serve many points.
  - Yield only in-region points when cheap (`ctx.in_region(key)`); the runner drops the rest anyway.
- Use `ctx.source_config` for URLs, ids and mappings; do not hardcode what the config carries.
- Documents are immutable snapshots; parsing must be deterministic.

## Tests

`tests/test_ingest_<slug>.py` with small fixtures under `tests/fixtures/<slug>/` (trim real downloads to
the header plus a few rows for our geographies; keep each file under ~50 KB). Use `FakeCtx` and
`doc_from_file`. Assert the expected points (geo keys, periods, values), that every excerpt equals
`doc.text()[start:end]`, and that out-of-region rows are skipped. Run
`uv run pytest -q tests/test_ingest_<slug>.py` and `uv run ruff check argusvue tests`.

## Live run

```bash
export UV_SYSTEM_CERTS=1
uv run argusvue ingest <slug> --region phx --backfill     # or --region jh, or --region '*' (national)
uv run argusvue ingest <slug> --region phx                # second run: inserted must be 0
uv run argusvue indicators show <indicator_id>            # series list
uv run argusvue indicators show <indicator_id> --geo <geo_key> --last 12
```

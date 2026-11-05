"""Runner contract on the synthetic gazetteer: idempotent re-runs, revisions, excerpt enforcement."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from argusvue.ingest import registry
from argusvue.ingest.base import FetchSpec, Point, StructuredSource, period_bounds
from argusvue.ingest.fetch import FixtureFetcher
from argusvue.ingest.runner import IngestError, run_ingest
from argusvue.store.blobs import BlobStore

pytestmark = pytest.mark.db

URL = "https://example.test/series.csv"


class CsvFixtureSource(StructuredSource):
    """indicator,zcta,period,value rows; excerpt = the raw line with offsets."""

    slug = "fixture_csv"

    def fetch_specs(self, ctx):
        return [FetchSpec(url=URL, key="series", suffix=".csv")]

    def parse(self, doc, ctx):
        for off, line in doc.iter_lines():
            if not line or line.startswith("indicator"):
                continue
            ind, z, period, value = line.split(",")
            key = ctx.zcta(z)
            if key is None:
                key = f"zcta:{z}"  # out of region: runner drops it
            yield Point(indicator_id=ind, geo_key=key, period=date.fromisoformat(period), value=float(value),
                        excerpt=line, excerpt_start=off, excerpt_end=off + len(line))


class BadOffsetsSource(CsvFixtureSource):
    slug = "fixture_bad"

    def parse(self, doc, ctx):
        for p in super().parse(doc, ctx):
            p.excerpt_start += 1
            yield p


class DuplicateSource(CsvFixtureSource):
    slug = "fixture_dup"

    def parse(self, doc, ctx):
        pts = list(super().parse(doc, ctx))
        yield from pts
        yield from pts[:1]


@pytest.fixture
def fixture_registry(monkeypatch, tmp_path):
    """Register the fixture source + indicator without touching the real config files."""
    src = registry.SourceConfig(slug="fixture_csv", name="Fixture", kind="structured", origin_key="fixture",
                                reliability=0.8, regions=["tst"], module=f"{__name__}:CsvFixtureSource")
    ind = registry.IndicatorConfig(id="fx_price", name="Fixture price", unit="USD", half_life_days=30,
                                   source="fixture_csv", frequency="monthly", regions=["tst"])
    monkeypatch.setattr(registry, "load_sources", lambda *a, **k: {"fixture_csv": src})
    monkeypatch.setattr(registry, "load_indicators", lambda *a, **k: {"fx_price": ind})
    return src


def _csv(tmp_path: Path, name: str, rows: list[str]) -> Path:
    p = tmp_path / name
    p.write_text("indicator,zcta,period,value\n" + "\n".join(rows) + "\n")
    return p


def _run(db, tmp_path, src, path, **kw):
    store = BlobStore(tmp_path / "blobs")
    fetcher = FixtureFetcher(store, {URL: path})
    return run_ingest(db, src.slug, "tst", source_cfg=src, fetcher=fetcher, store=store, **kw)


def _count(db, sql, params=None):
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()["n"]


def test_first_run_creates_cited_points_with_full_geo_ancestry(synthetic, fixture_registry, tmp_path):
    p = _csv(tmp_path, "v1.csv", ["fx_price,99001,2024-01-01,100", "fx_price,99001,2024-02-01,110",
                                  "fx_price,99002,2024-01-01,90", "fx_price,00000,2024-01-01,1"])
    st = _run(synthetic, tmp_path, fixture_registry, p)
    assert (st["parsed"], st["dropped_out_of_region"], st["inserted"], st["revised"], st["new_documents"]) == (4, 1, 3, 0, 1)
    assert _count(synthetic, "SELECT count(*) AS n FROM claims") == 3
    assert _count(synthetic, "SELECT count(*) AS n FROM indicator_points") == 3
    with synthetic.cursor() as cur:
        cur.execute("""SELECT c.*, e.geo_key AS ent_geo FROM claims c JOIN entities e ON e.id = c.subject_entity
                       WHERE c.geo_key = 'zcta:99001' AND c.valid_from = '2024-02-01'""")
        c = cur.fetchone()
        assert c["predicate"] == "indicator:fx_price" and c["value_num"] == 110 and c["ent_geo"] == "zcta:99001"
        assert c["valid_to"] == date(2024, 2, 29) and c["status"] == "asserted" and float(c["confidence"]) == pytest.approx(0.8)
        assert c["excerpt"] == "fx_price,99001,2024-02-01,110" and c["half_life_days"] == 30
        cur.execute("SELECT geo_key FROM claim_geo WHERE claim_id = %s ORDER BY level", (c["id"],))
        assert [r["geo_key"] for r in cur.fetchall()] == [
            "country:US", "state:99", "cbsa:99999", "county:99001", "place:9900001", "sub:test-sub", "zcta:99001"]
        cur.execute("SELECT origin_key FROM claim_origins WHERE claim_id = %s", (c["id"],))
        assert cur.fetchone()["origin_key"] == "fixture"
        cur.execute("SELECT ok, stats FROM job_runs ORDER BY id DESC LIMIT 1")
        jr = cur.fetchone()
        assert jr["ok"] and jr["stats"]["inserted"] == 3


def test_rerun_of_identical_document_adds_zero_rows(synthetic, fixture_registry, tmp_path):
    p = _csv(tmp_path, "v1.csv", ["fx_price,99001,2024-01-01,100", "fx_price,99002,2024-01-01,90"])
    _run(synthetic, tmp_path, fixture_registry, p)
    before = {t: _count(synthetic, f"SELECT count(*) AS n FROM {t}") for t in ("documents", "claims", "indicator_points", "claim_geo", "entities")}
    st = _run(synthetic, tmp_path, fixture_registry, p)
    assert st["skipped"] == 1 and st["inserted"] == 0 and st["new_documents"] == 0
    after = {t: _count(synthetic, f"SELECT count(*) AS n FROM {t}") for t in before}
    assert before == after
    # --force re-parses but still inserts nothing: values are unchanged
    st = _run(synthetic, tmp_path, fixture_registry, p, force=True)
    assert st["skipped"] == 0 and st["inserted"] == 0 and st["unchanged"] == 2
    assert {t: _count(synthetic, f"SELECT count(*) AS n FROM {t}") for t in before} == before


def test_revised_value_is_a_new_revision_and_supersedes_old_claim(synthetic, fixture_registry, tmp_path):
    p1 = _csv(tmp_path, "v1.csv", ["fx_price,99001,2024-01-01,100", "fx_price,99001,2024-02-01,110"])
    _run(synthetic, tmp_path, fixture_registry, p1)
    p2 = _csv(tmp_path, "v2.csv", ["fx_price,99001,2024-01-01,100", "fx_price,99001,2024-02-01,115",
                                   "fx_price,99001,2024-03-01,120"])
    st = _run(synthetic, tmp_path, fixture_registry, p2)
    assert (st["new_documents"], st["inserted"], st["revised"], st["unchanged"]) == (1, 2, 1, 1)
    assert _count(synthetic, "SELECT count(*) AS n FROM indicator_points") == 4      # revisions kept
    assert _count(synthetic, "SELECT count(*) AS n FROM indicator_latest") == 3      # current belief
    with synthetic.cursor() as cur:
        cur.execute("SELECT value, claim_id FROM indicator_latest WHERE geo_key='zcta:99001' AND period='2024-02-01'")
        cur_row = cur.fetchone()
        assert cur_row["value"] == 115
        cur.execute("SELECT id, status, superseded_by FROM claims WHERE geo_key='zcta:99001' AND valid_from='2024-02-01' ORDER BY id")
        old, new = cur.fetchall()
        assert old["status"] == "superseded" and old["superseded_by"] == new["id"] == cur_row["claim_id"]
        assert new["status"] == "asserted"
        cur.execute("SELECT count(*) AS n FROM indicator_points WHERE geo_key='zcta:99001' AND period='2024-02-01'")
        assert cur.fetchone()["n"] == 2


def test_excerpt_offsets_must_match_document(synthetic, fixture_registry, monkeypatch, tmp_path):
    src = registry.SourceConfig(**{**fixture_registry.model_dump(), "slug": "fixture_bad", "module": f"{__name__}:BadOffsetsSource"})
    p = _csv(tmp_path, "v1.csv", ["fx_price,99001,2024-01-01,100"])
    with pytest.raises(IngestError, match="excerpt offsets"):
        _run(synthetic, tmp_path, src, p)
    assert _count(synthetic, "SELECT count(*) AS n FROM claims") == 0
    with synthetic.cursor() as cur:
        cur.execute("SELECT ok, error FROM job_runs ORDER BY id DESC LIMIT 1")
        jr = cur.fetchone()
        assert jr["ok"] is False and "excerpt offsets" in jr["error"]


def test_duplicate_series_point_in_one_document_fails_loudly(synthetic, fixture_registry, tmp_path):
    src = registry.SourceConfig(**{**fixture_registry.model_dump(), "slug": "fixture_dup", "module": f"{__name__}:DuplicateSource"})
    p = _csv(tmp_path, "v1.csv", ["fx_price,99001,2024-01-01,100"])
    with pytest.raises(IngestError, match="duplicate"):
        _run(synthetic, tmp_path, src, p)


def test_unknown_indicator_fails_loudly(synthetic, fixture_registry, tmp_path):
    p = _csv(tmp_path, "v1.csv", ["fx_nope,99001,2024-01-01,100"])
    with pytest.raises(IngestError, match="unknown indicator"):
        _run(synthetic, tmp_path, fixture_registry, p)


@pytest.mark.parametrize(
    "freq,period,vf,vt",
    [
        ("monthly", date(2024, 2, 10), date(2024, 2, 1), date(2024, 2, 29)),
        ("quarterly", date(2024, 11, 1), date(2024, 10, 1), date(2024, 12, 31)),
        ("annual", date(2023, 1, 1), date(2023, 1, 1), date(2023, 12, 31)),
        ("weekly", date(2024, 3, 7), date(2024, 3, 1), date(2024, 3, 7)),
        ("daily", date(2024, 3, 7), date(2024, 3, 7), date(2024, 3, 7)),
    ],
)
def test_period_bounds(freq, period, vf, vt):
    assert period_bounds(freq, period) == (vf, vt)

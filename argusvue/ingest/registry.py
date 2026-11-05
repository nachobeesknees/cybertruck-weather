"""Typed loaders for config/sources.yaml and config/indicators.yaml, plus DB sync (idempotent upserts)."""

from __future__ import annotations

import importlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from argusvue.ingest.base import FREQUENCIES, StructuredSource
from argusvue.settings import CONFIG_DIR

SOURCE_KINDS = ("structured", "web_monitor", "web_task", "own_fetch", "pdf", "feed", "search")


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    kind: str
    origin_key: str
    url: str | None = None
    cadence: str | None = None            # Postgres interval text, e.g. '1 month'
    reliability: float = 0.5
    license_note: str | None = None
    regions: list[str] = Field(default_factory=lambda: ["*"])   # region ids, or '*' for national
    module: str | None = None             # 'argusvue.ingest.zillow:ZillowSource' for structured sources
    config: dict[str, Any] = Field(default_factory=dict)
    active: bool = True
    robots_disallowed: bool = False       # host robots.txt disallows crawlers for this path (see DECISIONS)
    request_delay_s: float = 1.0          # minimum gap between requests to the host

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in SOURCE_KINDS:
            raise ValueError(f"kind must be one of {SOURCE_KINDS}, got {v!r}")
        return v

    @field_validator("reliability")
    @classmethod
    def _rel(cls, v: float) -> float:
        if not 0 <= v <= 1:
            raise ValueError("reliability must be within [0, 1]")
        return v

    def applies_to(self, region_id: str) -> bool:
        return "*" in self.regions or region_id in self.regions


class IndicatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    unit: str
    direction_good: str = "neutral"
    half_life_days: int
    source: str
    frequency: str
    regions: list[str] = Field(default_factory=lambda: ["*"])
    description: str | None = None
    low_confidence: bool = False
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("direction_good")
    @classmethod
    def _dir(cls, v: str) -> str:
        if v not in ("up", "down", "neutral"):
            raise ValueError("direction_good must be up|down|neutral")
        return v

    @field_validator("frequency")
    @classmethod
    def _freq(cls, v: str) -> str:
        if v not in FREQUENCIES:
            raise ValueError(f"frequency must be one of {FREQUENCIES}")
        return v


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def load_sources(path: Path = CONFIG_DIR / "sources.yaml") -> dict[str, SourceConfig]:
    raw = _read_yaml(path).get("sources") or {}
    return {slug: SourceConfig(slug=slug, **(body or {})) for slug, body in raw.items()}


@lru_cache
def load_indicators(path: Path = CONFIG_DIR / "indicators.yaml") -> dict[str, IndicatorConfig]:
    raw = _read_yaml(path).get("indicators") or {}
    out = {iid: IndicatorConfig(id=iid, **(body or {})) for iid, body in raw.items()}
    sources = load_sources()
    for ind in out.values():
        if ind.source not in sources:
            raise ValueError(f"indicator {ind.id} references unknown source {ind.source!r}")
    return out


def source(slug: str) -> SourceConfig:
    sources = load_sources()
    if slug not in sources:
        raise KeyError(f"unknown source {slug!r}; known: {sorted(sources)}")
    return sources[slug]


def source_class(cfg: SourceConfig) -> type[StructuredSource]:
    if not cfg.module:
        raise ValueError(f"source {cfg.slug} has no ingest module (kind={cfg.kind})")
    mod_name, _, cls_name = cfg.module.partition(":")
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    if not issubclass(cls, StructuredSource):
        raise TypeError(f"{cfg.module} is not a StructuredSource")
    return cls


def upsert_source(conn: psycopg.Connection, s: SourceConfig) -> None:
    with conn.cursor() as cur:
        cur.execute(
                """INSERT INTO sources(slug, name, kind, region_id, url, cadence, reliability, license_note, origin_key, active, config)
                   VALUES (%s, %s, %s, %s, %s, %s::interval, %s, %s, %s, %s, %s)
                   ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name, kind=EXCLUDED.kind, region_id=EXCLUDED.region_id,
                     url=EXCLUDED.url, cadence=EXCLUDED.cadence, reliability=EXCLUDED.reliability,
                     license_note=EXCLUDED.license_note, origin_key=EXCLUDED.origin_key, active=EXCLUDED.active,
                     config=EXCLUDED.config, updated_at=now()""",
            (s.slug, s.name, s.kind, s.regions[0] if len(s.regions) == 1 and s.regions[0] != "*" else None,
             s.url, s.cadence, s.reliability, s.license_note, s.origin_key, s.active, Jsonb(s.config)),
        )


def sync(conn: psycopg.Connection, extra_sources: list[SourceConfig] | None = None) -> dict[str, int]:
    """Upsert sources and indicators from config. Safe to run before every ingest."""
    sources = load_sources()
    indicators = load_indicators()
    for s in list(sources.values()) + list(extra_sources or []):
        upsert_source(conn, s)
    with conn.cursor() as cur:
        for i in indicators.values():
            cur.execute(
                """INSERT INTO indicators(id, name, unit, direction_good, half_life_days, source_id, region_scope, frequency,
                                          description, low_confidence, config)
                   VALUES (%s, %s, %s, %s, %s, (SELECT id FROM sources WHERE slug = %s), %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, unit=EXCLUDED.unit, direction_good=EXCLUDED.direction_good,
                     half_life_days=EXCLUDED.half_life_days, source_id=EXCLUDED.source_id, region_scope=EXCLUDED.region_scope,
                     frequency=EXCLUDED.frequency, description=EXCLUDED.description, low_confidence=EXCLUDED.low_confidence,
                     config=EXCLUDED.config, updated_at=now()""",
                (i.id, i.name, i.unit, i.direction_good, i.half_life_days, i.source, i.regions, i.frequency,
                 i.description, i.low_confidence, Jsonb(i.config)),
            )
    return {"sources": len(sources), "indicators": len(indicators)}

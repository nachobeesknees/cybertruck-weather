"""Typed loaders for config/*.yaml. Strict: unknown keys and malformed codes fail at load."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argusvue.settings import CONFIG_DIR

_FIPS_STATE = re.compile(r"^\d{2}$")
_FIPS_COUNTY = re.compile(r"^\d{5}$")
_FIPS_PLACE = re.compile(r"^\d{7}$")
_CBSA = re.compile(r"^\d{5}$")


class RegionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    cbsa: str
    states: list[str]
    counties: list[str]
    commuter_shed_counties: list[str] = Field(default_factory=list)
    places: list[str] = Field(default_factory=list)

    @field_validator("cbsa")
    @classmethod
    def _cbsa(cls, v: str) -> str:
        if not _CBSA.match(v):
            raise ValueError(f"cbsa code must be 5 digits: {v!r}")
        return v

    @field_validator("states")
    @classmethod
    def _states(cls, v: list[str]) -> list[str]:
        for s in v:
            if not _FIPS_STATE.match(s):
                raise ValueError(f"state FIPS must be 2 digits: {s!r}")
        return v

    @field_validator("counties", "commuter_shed_counties")
    @classmethod
    def _counties(cls, v: list[str]) -> list[str]:
        for c in v:
            if not _FIPS_COUNTY.match(c):
                raise ValueError(f"county FIPS must be 5 digits: {c!r}")
        return v

    @field_validator("places")
    @classmethod
    def _places(cls, v: list[str]) -> list[str]:
        for p in v:
            if not _FIPS_PLACE.match(p):
                raise ValueError(f"place code must be state(2)+place(5) = 7 digits: {p!r}")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> RegionConfig:
        all_counties = self.counties + self.commuter_shed_counties
        if len(set(all_counties)) != len(all_counties):
            raise ValueError(f"region {self.id}: duplicate counties")
        for c in all_counties:
            if c[:2] not in self.states:
                raise ValueError(f"region {self.id}: county {c} not in listed states {self.states}")
        for p in self.places:
            if p[:2] not in self.states:
                raise ValueError(f"region {self.id}: place {p} not in listed states {self.states}")
        return self

    @property
    def all_counties(self) -> list[str]:
        return self.counties + self.commuter_shed_counties


class Vintage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tiger: str
    delineation: str


class RegionsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vintage: Vintage
    regions: dict[str, RegionConfig]


class SubmarketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    region: str
    name: str
    members: list[str]

    @field_validator("members")
    @classmethod
    def _members(cls, v: list[str]) -> list[str]:
        from argusvue.geo.keys import parse_key  # local import to avoid a cycle

        for m in v:
            k = parse_key(m)
            if k.level not in ("zcta", "place"):
                raise ValueError(f"submarket members must be zcta: or place: keys, got {m!r}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate submarket members")
        return v

    @property
    def geo_key(self) -> str:
        return f"sub:{self.id}"


class SubmarketsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submarkets: dict[str, SubmarketConfig]


class ModelsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: dict[str, str]


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def load_regions(path: Path = CONFIG_DIR / "regions.yaml") -> RegionsFile:
    raw = _read_yaml(path)
    regions = {rid: {"id": rid, **(body or {})} for rid, body in (raw.get("regions") or {}).items()}
    return RegionsFile(vintage=raw.get("vintage"), regions=regions)


@lru_cache
def load_submarkets(path: Path = CONFIG_DIR / "submarkets.yaml") -> SubmarketsFile:
    raw = _read_yaml(path)
    subs = {sid: {"id": sid, **(body or {})} for sid, body in (raw.get("submarkets") or {}).items()}
    f = SubmarketsFile(submarkets=subs)
    known = set(load_regions().regions)
    for s in f.submarkets.values():
        if s.region not in known:
            raise ValueError(f"submarket {s.id} references unknown region {s.region!r}")
    return f


@lru_cache
def load_models(path: Path = CONFIG_DIR / "models.yaml") -> ModelsFile:
    return ModelsFile(**_read_yaml(path))


def region(region_id: str) -> RegionConfig:
    regions = load_regions().regions
    if region_id not in regions:
        raise KeyError(f"unknown region {region_id!r}; known: {sorted(regions)}")
    return regions[region_id]

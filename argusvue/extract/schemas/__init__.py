"""Typed extraction output (SPEC §6). The model proposes; code validates predicates, excerpts, persons."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DOMAINS = ("housing", "employment", "policy", "infrastructure", "water", "tourism")
ENTITY_TYPES = ("place", "parcel", "project", "org", "person", "instrument", "event")
_PRED_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@lru_cache
def vocab(path: Path = Path(__file__).parent / "vocab.yaml") -> dict[str, dict[str, dict]]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def known_predicates() -> dict[str, dict]:
    return {p: {**body, "domain": d} for d, preds in vocab().items() for p, body in preds.items()}


def vocab_text() -> str:
    lines = []
    for d, preds in vocab().items():
        lines.append(f"## {d}")
        for p, body in preds.items():
            extra = f" ({body.get('desc')})" if body.get("desc") else ""
            unit = f" unit={body['unit']}" if body.get("unit") else ""
            lines.append(f"- {p}: value={body['value']}{unit}{extra}")
    return "\n".join(lines)


class EntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, description="Name as the document gives it")
    type: Literal["place", "parcel", "project", "org", "person", "instrument", "event"]
    place_hint: str | None = Field(default=None, description="Where it is: 'Phoenix, AZ', 'ZIP 85085', 'Teton County, WY', 'Wilson'")
    address: str | None = None
    role: str | None = Field(default=None, description="Required for persons: public office or executive title")
    identifiers: dict[str, str] = Field(default_factory=dict, description="e.g. {'apn': '211-12-345'}")


class ExtractedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["housing", "employment", "policy", "infrastructure", "water", "tourism"]
    subject: EntityRef
    predicate: str
    object: EntityRef | None = None
    value_num: float | None = None
    value_text: str | None = None
    unit: str | None = None
    valid_from: str | None = Field(default=None, description="YYYY-MM-DD the fact holds from")
    valid_to: str | None = Field(default=None, description="YYYY-MM-DD the fact holds to, if bounded")
    excerpt: str = Field(min_length=10, max_length=600, description="Verbatim contiguous quote from the document that states the fact")
    confidence: float = Field(ge=0, le=1)

    @field_validator("predicate")
    @classmethod
    def _pred(cls, v: str) -> str:
        v = v.strip().lower()
        if not _PRED_RE.match(v):
            raise ValueError(f"predicate must look like 'domain.fact' in snake_case, got {v!r}")
        return v

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v is not None and not _DATE_RE.match(v):
            raise ValueError(f"dates must be YYYY-MM-DD, got {v!r}")
        return v


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[ExtractedClaim] = Field(default_factory=list)
    document_summary: str = ""
    primary_geo_hint: str | None = Field(default=None, description="The place the document is mainly about")
    published_date: str | None = Field(default=None, description="YYYY-MM-DD if stated in the document")


def validate_claim(c: ExtractedClaim) -> list[str]:
    """Semantic checks beyond the schema. Returns a list of problems (empty = ok)."""
    errs: list[str] = []
    kp = known_predicates()
    if c.predicate in kp:
        vt = kp[c.predicate]["value"]
        if vt == "num" and c.value_num is None:
            errs.append(f"{c.predicate} needs value_num")
        if vt == "text" and not (c.value_text or "").strip():
            errs.append(f"{c.predicate} needs value_text")
        if vt == "entity" and c.object is None:
            errs.append(f"{c.predicate} needs an object entity")
        if vt == "date" and not (c.value_text and _DATE_RE.match(c.value_text)):
            errs.append(f"{c.predicate} needs value_text as YYYY-MM-DD")
    elif c.value_num is None and not (c.value_text or "").strip() and c.object is None:
        errs.append(f"{c.predicate} has no value")
    if c.subject.type == "person" and not (c.subject.role or "").strip():
        errs.append("person subjects must carry a public role (official or executive); private individuals are not recorded")
    if c.object is not None and c.object.type == "person" and not (c.object.role or "").strip():
        errs.append("person objects must carry a public role")
    return errs

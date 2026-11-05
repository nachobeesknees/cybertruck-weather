"""Structured outputs for the thesis review loop (SPEC §8). The model proposes; the Validator checks ids,
entities, falsifiers and entailment in code; the Critic sees the proposal and packet only."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ThesisStatus = Literal["emerging", "established", "weakening", "dead"]


class TriageVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material: bool
    reason: str = Field(min_length=3)


class ConfidenceReasoning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    independence: str = ""
    recency: str = ""
    corroboration: str = ""
    mechanism: str = ""
    base_rate: str = ""


class ProposedEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cause: str
    effect: str
    mechanism: str
    lag_months: int = 0
    strength: Literal["weak", "moderate", "strong"] = "moderate"
    evidence: list[int] = Field(default_factory=list, description="claim ids from the allowlist")
    status: Literal["hypothesis", "tested"] = "hypothesis"
    cause_indicator: str | None = None
    effect_indicator: str | None = None
    geo_key: str | None = None


class ProposedImplication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=5)
    cites: list[int | str] = Field(default_factory=list, description="claim ids or 'indicator_id@geo_key' from the allowlist")
    horizon_months: int = 0
    hypothesis: bool = False


class ProposedFalsifier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indicator_id: str
    geo_key: str
    operator: Literal["<", ">", "<=", ">="]
    threshold: float
    by_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    action: Literal["downgrade", "retire", "review"]
    description: str = ""


class ProposedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=5)
    collector_task: dict = Field(default_factory=dict, description="{objective, geo_scope, kind: search|page|feed}")


class AnalystProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["no_change", "update", "split", "retire", "propose_new"]
    statement: str = Field(min_length=10)
    status: ThesisStatus
    confidence: float = Field(ge=0, le=1)
    confidence_reasoning: ConfidenceReasoning
    support: list[int] = Field(default_factory=list)
    counter: list[int] = Field(default_factory=list)
    causal_edges: list[ProposedEdge] = Field(default_factory=list)
    implications: list[ProposedImplication] = Field(default_factory=list)
    falsifiers: list[ProposedFalsifier] = Field(default_factory=list)
    open_questions: list[ProposedQuestion] = Field(default_factory=list)
    change_summary: str = Field(min_length=5)

    @field_validator("support", "counter")
    @classmethod
    def _dedupe(cls, v: list[int]) -> list[int]:
        return sorted(set(v))


class CriticEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    change: str
    reason: str = ""


class CriticReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rival_mechanisms: list[str] = Field(default_factory=list, description="one per causal edge, in order")
    base_rate_check: str = ""
    source_audit: dict[str, str] = Field(default_factory=dict, description="independence, incentives, specificity")
    missing_evidence: list[str] = Field(default_factory=list)
    overreach: list[str] = Field(default_factory=list)
    consistency: str = ""
    verdict: Literal["approve", "revise", "reject"]
    edits: list[CriticEdit] = Field(default_factory=list)
    collector_tasks: list[ProposedQuestion] = Field(default_factory=list)
    summary: str = ""


class EntailmentVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["entailed", "neutral", "contradicted"]
    reason: str = ""

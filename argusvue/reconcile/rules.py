"""Pure reconciliation rules (no DB): predicate classes, value comparison, the decision table."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml

from argusvue.settings import CONFIG_DIR


@dataclass(frozen=True)
class PredicateClass:
    name: str
    half_life_days: int
    tolerance_rel: float = 0.0
    tolerance_abs: float = 0.0


@lru_cache
def _load(path: Path = CONFIG_DIR / "predicates.yaml") -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def confidence_cap() -> float:
    return float(_load().get("confidence_cap", 0.95))


@lru_cache
def predicate_class(predicate: str) -> PredicateClass:
    cfg = _load()
    for name, body in (cfg.get("classes") or {}).items():
        for pat in body.get("patterns", []):
            if re.search(pat, predicate):
                return PredicateClass(name, int(body["half_life_days"]), float(body.get("tolerance_rel", 0.0)),
                                      float(body.get("tolerance_abs", 0.0)))
    d = cfg.get("default") or {}
    return PredicateClass("default", int(d.get("half_life_days", 180)), float(d.get("tolerance_rel", 0.0)),
                          float(d.get("tolerance_abs", 0.0)))


def half_life_for(predicate: str) -> int:
    return predicate_class(predicate).half_life_days


def combined_confidence(reliabilities: list[float], cap: float | None = None) -> float:
    """conf = 1 - prod(1 - r_i) over distinct origins, capped (SPEC §6)."""
    cap = confidence_cap() if cap is None else cap
    p = 1.0
    for r in reliabilities:
        p *= 1.0 - max(0.0, min(1.0, float(r)))
    return round(min(cap, 1.0 - p), 4)


def effective_confidence(confidence: float, age_days: float, half_life_days: int | None) -> float:
    if not half_life_days or half_life_days <= 0:
        return float(confidence)
    return float(confidence) * 0.5 ** (max(age_days, 0.0) / half_life_days)


def _overlap(a_from: date | None, a_to: date | None, b_from: date | None, b_to: date | None) -> bool:
    lo = max(a_from or date.min, b_from or date.min)
    hi = min(a_to or date.max, b_to or date.max)
    return lo <= hi


@dataclass(frozen=True)
class Decision:
    action: str          # corroborate | merge | contest | supersede | ignore
    reason: str


def compare_values(predicate: str, new_num: Decimal | None, new_text: str | None, new_obj: int | None,
                   old_num: Decimal | None, old_text: str | None, old_obj: int | None) -> str:
    """'agree' | 'within_tolerance' | 'contradict'."""
    pc = predicate_class(predicate)
    if new_num is not None and old_num is not None:
        if new_num == old_num:
            return "agree"
        tol = max(abs(float(old_num)) * pc.tolerance_rel, pc.tolerance_abs)
        return "within_tolerance" if abs(float(new_num) - float(old_num)) <= tol else "contradict"
    if new_obj is not None and old_obj is not None:
        return "agree" if new_obj == old_obj else "contradict"
    a = (new_text or "").strip().lower()
    b = (old_text or "").strip().lower()
    if a and b:
        return "agree" if a == b else "contradict"
    if (new_num is None) != (old_num is None) and (a or b):
        return "contradict"
    return "agree" if (a == b and new_num == old_num and new_obj == old_obj) else "contradict"


def decide(predicate: str, *, new_valid_from: date | None, new_valid_to: date | None, new_num, new_text, new_obj,
           old_valid_from: date | None, old_valid_to: date | None, old_num, old_text, old_obj, old_status: str) -> Decision:
    """Decision table from SPEC §6 for one (new, existing) pair with the same subject + predicate."""
    if old_status in ("retracted", "superseded"):
        return Decision("ignore", f"existing claim is {old_status}")
    if not _overlap(new_valid_from, new_valid_to, old_valid_from, old_valid_to):
        if new_valid_from and old_valid_from and new_valid_from > old_valid_from and old_valid_to is None:
            return Decision("supersede", "newer valid_from; existing claim was open-ended")
        return Decision("ignore", "valid periods do not overlap")
    # Overlapping periods: a strictly newer valid_from with an open-ended existing claim is a supersession
    if new_valid_from and old_valid_from and new_valid_from > old_valid_from and old_valid_to is None:
        return Decision("supersede", "newer valid_from supersedes the open-ended existing claim")
    cmp = compare_values(predicate, new_num, new_text, new_obj, old_num, old_text, old_obj)
    if cmp == "agree":
        return Decision("corroborate", "same value")
    if cmp == "within_tolerance":
        return Decision("merge", "numeric difference within tolerance")
    return Decision("contest", "values contradict")

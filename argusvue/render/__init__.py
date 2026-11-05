"""Rendering layer (SPEC §9): Markdown briefs and place pages built only from claim/indicator ids."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RenderResult:
    path: Path
    dropped: list[str] = field(default_factory=list)   # sentences the tag enforcer refused
    stats: dict[str, Any] = field(default_factory=dict)

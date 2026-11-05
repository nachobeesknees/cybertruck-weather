"""geo_key = '{level}:{code}'. Pure functions, no DB."""

from __future__ import annotations

import re
from dataclasses import dataclass

import h3

# Broad -> fine. 'h3' sits outside the admin hierarchy and is never an ancestor by containment.
LEVELS: tuple[str, ...] = ("country", "state", "cbsa", "county", "place", "sub", "zcta", "tract", "parcel", "h3")
LEVEL_RANK: dict[str, int] = {lvl: i for i, lvl in enumerate(LEVELS)}
ADMIN_LEVELS: tuple[str, ...] = LEVELS[:-1]

H3_RESOLUTIONS: tuple[int, ...] = (7, 8, 9)

_CODE_RULES: dict[str, re.Pattern[str]] = {
    "country": re.compile(r"^[A-Z]{2}$"),
    "state": re.compile(r"^\d{2}$"),
    "cbsa": re.compile(r"^\d{5}$"),
    "county": re.compile(r"^\d{5}$"),
    "place": re.compile(r"^\d{7}$"),
    "sub": re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$"),
    "zcta": re.compile(r"^\d{5}$"),
    "tract": re.compile(r"^\d{11}$"),
    "parcel": re.compile(r"^\d{5}:[A-Za-z0-9.\-]+$"),  # county FIPS ':' APN
    "h3": re.compile(r"^[0-9a-f]{15}$"),
}


class GeoKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GeoKey:
    level: str
    code: str

    def __str__(self) -> str:
        return f"{self.level}:{self.code}"

    @property
    def rank(self) -> int:
        return LEVEL_RANK[self.level]

    def is_coarser_than(self, other: GeoKey) -> bool:
        return self.rank < other.rank


def parse_key(s: str) -> GeoKey:
    if not isinstance(s, str) or ":" not in s:
        raise GeoKeyError(f"geo_key must look like 'level:code', got {s!r}")
    level, _, code = s.partition(":")
    if level not in LEVEL_RANK:
        raise GeoKeyError(f"unknown level {level!r} in {s!r}; levels: {LEVELS}")
    rule = _CODE_RULES[level]
    if not rule.match(code):
        raise GeoKeyError(f"code {code!r} is not valid for level {level!r} (pattern {rule.pattern})")
    if level == "h3" and not h3.is_valid_cell(code):
        raise GeoKeyError(f"{code!r} is not a valid H3 cell")
    return GeoKey(level, code)


def make_key(level: str, code: str) -> str:
    return str(parse_key(f"{level}:{code}"))


def is_valid_key(s: str) -> bool:
    try:
        parse_key(s)
    except GeoKeyError:
        return False
    return True


def h3_keys(lat: float, lon: float, resolutions: tuple[int, ...] = H3_RESOLUTIONS) -> list[str]:
    """H3 cell keys for a point at the configured resolutions, coarse to fine."""
    return [f"h3:{h3.latlng_to_cell(lat, lon, r)}" for r in resolutions]


def h3_center(cell: str) -> tuple[float, float]:
    """(lat, lon) centre of an H3 cell."""
    return h3.cell_to_latlng(cell)


# Structural parents derivable from the code alone (no geometry needed).
def code_parent(key: GeoKey) -> GeoKey | None:
    if key.level == "tract":
        return GeoKey("county", key.code[:5])
    if key.level == "county":
        return GeoKey("state", key.code[:2])
    if key.level == "place":
        return GeoKey("state", key.code[:2])
    if key.level == "parcel":
        return GeoKey("county", key.code[:5])
    if key.level == "state":
        return GeoKey("country", "US")
    return None

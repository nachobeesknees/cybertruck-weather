"""Census TIGER/Line + OMB delineation downloads. Files are cached under data/downloads/<vintage>/."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from argusvue.settings import USER_AGENT, get_settings

TIGER_BASE = "https://www2.census.gov/geo/tiger"
DELINEATION_URL = (
    "https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2023/"
    "delineation-files/list1_2023.xlsx"
)


@dataclass(frozen=True)
class TigerFile:
    layer: str          # state | cbsa | county | place | zcta | tract
    scope: str          # 'us' or a 2-digit state FIPS
    filename: str
    url: str


def tiger_files(vintage: str, states: list[str]) -> list[TigerFile]:
    """The files needed for the given states. National files for state/cbsa/county/zcta; per-state for
    place/tract."""
    year = vintage.removeprefix("TIGER")
    out = [
        TigerFile("state", "us", f"tl_{year}_us_state.zip", f"{TIGER_BASE}/{vintage}/STATE/tl_{year}_us_state.zip"),
        TigerFile("cbsa", "us", f"tl_{year}_us_cbsa.zip", f"{TIGER_BASE}/{vintage}/CBSA/tl_{year}_us_cbsa.zip"),
        TigerFile("county", "us", f"tl_{year}_us_county.zip", f"{TIGER_BASE}/{vintage}/COUNTY/tl_{year}_us_county.zip"),
        TigerFile("zcta", "us", f"tl_{year}_us_zcta520.zip", f"{TIGER_BASE}/{vintage}/ZCTA520/tl_{year}_us_zcta520.zip"),
    ]
    for st in sorted(set(states)):
        out.append(TigerFile("place", st, f"tl_{year}_{st}_place.zip", f"{TIGER_BASE}/{vintage}/PLACE/tl_{year}_{st}_place.zip"))
        out.append(TigerFile("tract", st, f"tl_{year}_{st}_tract.zip", f"{TIGER_BASE}/{vintage}/TRACT/tl_{year}_{st}_tract.zip"))
    return out


def download_dir(vintage: str) -> Path:
    d = get_settings().downloads_dir / vintage.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch(url: str, dest: Path, *, timeout: float = 600.0) -> Path:
    """Stream a file to disk if not already present. Writes to a temp name then renames, so a partial
    download is never mistaken for a complete one."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    with (
        httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True) as client,
        client.stream("GET", url) as r,
    ):
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def ensure_files(vintage: str, states: list[str]) -> dict[str, Path]:
    """Return {filename: path} for all needed TIGER files plus the delineation workbook, downloading
    any that are missing."""
    d = download_dir(vintage)
    paths: dict[str, Path] = {}
    for tf in tiger_files(vintage, states):
        paths[tf.filename] = fetch(tf.url, d / tf.filename)
    paths["delineation"] = fetch(DELINEATION_URL, d / "list1_2023.xlsx")
    return paths

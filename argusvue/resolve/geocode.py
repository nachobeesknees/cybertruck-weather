"""Geo resolution for claims: gazetteer names first (places, counties, ZIPs, submarkets), Census geocoder for
street addresses. Returns gazetteer keys only."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

import httpx

from argusvue.geo.states import USPS_BY_FIPS
from argusvue.ingest.base import IngestContext
from argusvue.settings import USER_AGENT

log = logging.getLogger(__name__)

_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_COUNTY = re.compile(r"\b([A-Za-z .'-]+?)\s+County\b", re.IGNORECASE)
_STATE_SUFFIX = re.compile(r",\s*([A-Za-z]{2}|Arizona|Wyoming|Idaho)\.?$|\s+(AZ|WY|ID|Arizona|Wyoming|Idaho)\.?$", re.IGNORECASE)
_PREFIX = re.compile(r"^(the\s+)?(city|town|village)\s+of\s+", re.IGNORECASE)
GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"


def _state_usps_list(ctx: IngestContext) -> list[str]:
    return [USPS_BY_FIPS[s] for s in ctx.state_fips if s in USPS_BY_FIPS]


def geocode_hint(ctx: IngestContext, hint: str | None) -> str | None:
    """Place/ZIP/county name -> geo_key in the region's gazetteer, or None."""
    if not hint:
        return None
    h = hint.strip()
    m = _ZIP.search(h)
    if m and (key := ctx.zcta(m.group(1))):
        return key
    if (m := _COUNTY.search(h)):
        name = f"{m.group(1).strip()} County"
        for st in _state_usps_list(ctx):
            if (key := ctx.county_by_name(name, st)):
                return key
    name = _PREFIX.sub("", _STATE_SUFFIX.sub("", h)).strip(" ,.")
    for st in _state_usps_list(ctx):
        if (key := ctx.place_by_name(name, st)):
            return key
    # submarket by id-ish name ("north phoenix", "teton village")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    for key in ctx.geo_keys:
        if key.startswith("sub:") and key.split(":", 1)[1].endswith(slug):
            return key
    return None


def geocode_address(ctx: IngestContext, address: str, *, client: httpx.Client | None = None) -> str | None:
    """Street address -> tract key (or county) via the Census geocoder; None when unmatched."""
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0)
    q = urlencode({"address": address, "benchmark": "Public_AR_Current", "vintage": "Current_Current", "format": "json"})
    try:
        r = client.get(f"{GEOCODER}?{q}")
        r.raise_for_status()
        matches = r.json().get("result", {}).get("addressMatches", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("geocoder failed for %r: %s", address, e)
        return None
    if not matches:
        return None
    geos = matches[0].get("geographies", {})
    tracts = geos.get("Census Tracts") or []
    if tracts and (key := f"tract:{tracts[0]['GEOID']}") in ctx.geo_keys:
        return key
    counties = geos.get("Counties") or []
    if counties and (key := ctx.county(counties[0]["GEOID"])):
        return key
    return None


def resolve_geo(ctx: IngestContext, *, place_hint: str | None, address: str | None, fallbacks: list[str | None],
                client: httpx.Client | None = None) -> str:
    """Best gazetteer key for a claim: address > place hint > document-level hints > region CBSA."""
    if address and (key := geocode_address(ctx, address, client=client)):
        return key
    if (key := geocode_hint(ctx, place_hint)):
        return key
    for fb in fallbacks:
        if fb and (key := (fb if fb in ctx.geo_keys else geocode_hint(ctx, fb))):
            return key
    return f"cbsa:{ctx.cbsa_code}" if ctx.cbsa_code else "country:US"

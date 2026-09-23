"""Saved spots: places the user wants to fly next.

A spot is a point picked on the map, optionally with the DIPUL geo zones that
were found there when it was saved. The zones are a snapshot for orientation
only; they are never re-checked in the background.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

MAX_ZONES = 50
_ZONE_TEXT = vol.Any(None, vol.All(cv.string, vol.Length(max=300)))

ZONE_SCHEMA = vol.Schema(
    {vol.Optional(key): _ZONE_TEXT for key in ("layer", "name", "type", "lower", "upper", "legal")}
)

_FIELDS = {
    "name": vol.All(cv.string, vol.Strip, vol.Length(min=1, max=100)),
    "lat": vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)),
    "lon": vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
    "note": vol.All(cv.string, vol.Length(max=1000)),
    "zones": vol.Any(None, vol.All([ZONE_SCHEMA], vol.Length(max=MAX_ZONES))),
    "zones_checked": vol.Any(None, cv.string),
}

CREATE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): _FIELDS["name"],
        vol.Required("lat"): _FIELDS["lat"],
        vol.Required("lon"): _FIELDS["lon"],
        vol.Optional("note", default=""): _FIELDS["note"],
        vol.Optional("zones", default=None): _FIELDS["zones"],
        vol.Optional("zones_checked", default=None): _FIELDS["zones_checked"],
    }
)

UPDATE_SCHEMA = vol.Schema({vol.Optional(key): validator for key, validator in _FIELDS.items()})


def maps_url(spot: dict[str, Any]) -> str:
    """Google Maps directions to the spot; opens the Maps app on phones."""
    return f"https://www.google.com/maps/dir/?api=1&destination={spot['lat']:.6f},{spot['lon']:.6f}"


def new_spot(data: dict[str, Any]) -> dict[str, Any]:
    """Build a spot from validated ``CREATE_SCHEMA`` data."""
    return {
        "id": secrets.token_hex(6),
        "created": datetime.now(UTC).isoformat(),
        **data,
    }


def public(spot: dict[str, Any]) -> dict[str, Any]:
    return {**spot, "maps_url": maps_url(spot)}


def sorted_spots(spots: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first."""
    return sorted(spots.values(), key=lambda s: s.get("created", ""), reverse=True)

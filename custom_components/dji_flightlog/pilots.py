"""Pilots: the people who fly, so flights can be told apart per person.

A pilot has a name, optionally the Home Assistant user it belongs to (the
panel then starts with that user's own flights) and the aircraft it usually
flies. A flight gets its pilot either by hand or, failing that, from its
aircraft. Assignments by hand are kept apart from the flight summaries,
which are rewritten whenever a log is parsed again.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

# Query value of the flight filter: flights without a pilot.
NO_PILOT = "none"
# Query value of the flight filter: the pilot linked to the requesting user.
ME = "me"

SOURCE_MANUAL = "manual"
SOURCE_AIRCRAFT = "aircraft"

_FIELDS = {
    "name": vol.All(cv.string, vol.Strip, vol.Length(min=1, max=60)),
    "user_id": vol.Any(None, vol.All(cv.string, vol.Length(min=1, max=64))),
    "aircraft": vol.All([vol.All(cv.string, vol.Length(min=1, max=64))], vol.Length(max=50)),
}

CREATE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): _FIELDS["name"],
        vol.Optional("user_id", default=None): _FIELDS["user_id"],
        vol.Optional("aircraft", default=list): _FIELDS["aircraft"],
    }
)

UPDATE_SCHEMA = vol.Schema({vol.Optional(key): validator for key, validator in _FIELDS.items()})

# pilot_id: a pilot, None: explicitly nobody, "auto": back to the aircraft's pilot.
AUTO = "auto"
ASSIGN_SCHEMA = vol.Schema(
    {
        vol.Required("flight_ids"): vol.All([cv.string], vol.Length(min=1, max=10000)),
        vol.Required("pilot_id"): vol.Any(None, cv.string),
    }
)


def new_pilot(data: dict[str, Any]) -> dict[str, Any]:
    """Build a pilot from validated ``CREATE_SCHEMA`` data."""
    return {
        "id": secrets.token_hex(6),
        "created": datetime.now(UTC).isoformat(),
        **data,
    }


def claim(pilots: dict[str, dict[str, Any]], pilot: dict[str, Any]) -> None:
    """Make ``pilot`` the only pilot of its aircraft and of its HA user."""
    mine = set(pilot.get("aircraft") or [])
    for other in pilots.values():
        if other is pilot:
            continue
        if mine and set(other.get("aircraft") or []) & mine:
            other["aircraft"] = [sn for sn in other["aircraft"] if sn not in mine]
        if pilot.get("user_id") and other.get("user_id") == pilot["user_id"]:
            other["user_id"] = None


def sorted_pilots(pilots: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """By name."""
    return sorted(pilots.values(), key=lambda p: (p["name"].casefold(), p["id"]))


def pilot_of(
    flight: dict[str, Any],
    pilots: dict[str, dict[str, Any]],
    assigned: dict[str, str | None],
) -> tuple[str | None, str | None]:
    """``(pilot_id, source)`` of a flight: by hand first, else its aircraft's pilot."""
    if flight["flight_id"] in assigned:
        pilot_id = assigned[flight["flight_id"]]
        if pilot_id is None or pilot_id in pilots:
            return pilot_id, SOURCE_MANUAL
    sn = flight.get("aircraft_sn")
    if sn:
        for pilot in pilots.values():
            if sn in (pilot.get("aircraft") or []):
                return pilot["id"], SOURCE_AIRCRAFT
    return None, None


def with_pilot(
    flight: dict[str, Any],
    pilots: dict[str, dict[str, Any]],
    assigned: dict[str, str | None],
) -> dict[str, Any]:
    """A copy of the flight summary with ``pilot_id``, ``pilot_name`` and ``pilot_source``."""
    pilot_id, source = pilot_of(flight, pilots, assigned)
    return {
        **flight,
        "pilot_id": pilot_id,
        "pilot_name": pilots[pilot_id]["name"] if pilot_id else None,
        "pilot_source": source,
    }


def resolve(value: str, pilots: dict[str, dict[str, Any]], user_id: str | None) -> str | None:
    """Filter value (id, name, ``me`` or ``none``) -> pilot id, ``none``, or None if unknown."""
    if value == NO_PILOT:
        return NO_PILOT
    if value == ME:
        return next((p["id"] for p in pilots.values() if user_id and p.get("user_id") == user_id), None)
    if value in pilots:
        return value
    needle = value.casefold()
    return next((p["id"] for p in pilots.values() if p["name"].casefold() == needle), None)

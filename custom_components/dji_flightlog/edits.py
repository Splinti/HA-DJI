"""Corrections by hand to what a flight log says.

Some logs carry a wrong clock (a start in 2081) or no address. The user can
correct the start time and the place; the corrections are kept apart from the
flight summaries, which are rewritten whenever a log is parsed again, and laid
over them when the flights are published. A corrected start moves the end
along, so the duration stays as logged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

# Before DJI Fly logs existed / clearly a broken clock.
EARLIEST = datetime(2010, 1, 1, tzinfo=UTC)
FUTURE_SLACK = timedelta(days=1)


def _start_time(value: Any) -> str:
    parsed = dt_util.parse_datetime(cv.string(value))
    if parsed is None:
        raise vol.Invalid("start_time must be an ISO 8601 date and time")
    if parsed.tzinfo is None:
        raise vol.Invalid("start_time needs a time zone")
    if not EARLIEST <= parsed <= dt_util.utcnow() + FUTURE_SLACK:
        raise vol.Invalid("start_time must lie between 2010 and today")
    return parsed.astimezone(UTC).isoformat()


_PLACE = vol.All(cv.string, vol.Strip, vol.Length(max=120))

# A field set to None drops its correction, so the log's value shows again.
UPDATE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("start_time"): vol.Any(None, _start_time),
            vol.Optional("city"): vol.Any(None, _PLACE),
            vol.Optional("street"): vol.Any(None, _PLACE),
        }
    ),
    vol.Length(min=1),
)


def update(edits: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """The flight's corrections after applying a validated ``UPDATE_SCHEMA`` body."""
    out = {**edits, **data}
    return {key: value for key, value in out.items() if value is not None}


def apply(flight: dict[str, Any], edits: dict[str, Any] | None) -> dict[str, Any]:
    """The flight with its corrections, plus ``edited``: field -> the log's value."""
    if not edits:
        return {**flight, "edited": {}}
    out = {**flight, **edits, "edited": {key: flight.get(key) for key in edits}}
    if "start_time" in edits and flight.get("end_time"):
        shift = datetime.fromisoformat(edits["start_time"]) - datetime.fromisoformat(flight["start_time"])
        out["end_time"] = (datetime.fromisoformat(flight["end_time"]) + shift).isoformat()
    return out

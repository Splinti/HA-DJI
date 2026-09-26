"""Weather at the time of a flight from Open-Meteo (opt-in).

One hourly value set per flight: the hour closest to the middle of the flight
at the takeoff point. The Historical Weather API serves reanalysis data for
older days and fills the last few days from the weather models, so a single
endpoint covers flights from yesterday back to 1940. Wind is at 10 m and at
100 m (the closest level it has to the usual 120 m flight ceiling).

Only the rounded position (2 decimals, about 1 km) and the date leave Home
Assistant; Open-Meteo's grid is coarser than that anyway.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
SOURCE = "open-meteo"

# Open-Meteo variable -> key in the stored weather record.
HOURLY = {
    "wind_speed_10m": "wind_ms",
    "wind_direction_10m": "wind_dir",
    "wind_gusts_10m": "gust_ms",
    "wind_speed_100m": "wind_100m_ms",
    "wind_direction_100m": "wind_100m_dir",
    "temperature_2m": "temp_c",
    "cloud_cover": "cloud_pct",
    "precipitation": "precip_mm",
    "weather_code": "code",
}

_TIMEOUT = aiohttp.ClientTimeout(total=20)


class WeatherUnavailable(Exception):
    """Open-Meteo has no data for this place and day (yet); try again later."""


class WeatherError(Exception):
    """Open-Meteo could not be reached or failed; stop and retry on a later scan."""


def flight_position(flight: dict[str, Any]) -> tuple[float, float] | None:
    """Takeoff point, else the home point; None without a GPS fix."""
    for lat_key, lon_key in (("takeoff_lat", "takeoff_lon"), ("home_lat", "home_lon")):
        lat, lon = flight.get(lat_key), flight.get(lon_key)
        if lat is not None and lon is not None and (abs(lat) > 1e-6 or abs(lon) > 1e-6):
            return round(float(lat), 2), round(float(lon), 2)
    return None


def flight_moment(flight: dict[str, Any]) -> datetime:
    """The middle of the flight, UTC."""
    start = datetime.fromisoformat(flight["start_time"])
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start.astimezone(UTC) + timedelta(seconds=float(flight.get("duration_s") or 0.0) / 2)


def pick_hour(hourly: dict[str, list[Any]], moment: datetime) -> dict[str, Any] | None:
    """The stored record for the hour closest to ``moment``; None if Open-Meteo has no values for it."""
    times = hourly.get("time") or []
    target = moment.astimezone(UTC).replace(tzinfo=None)
    best, best_diff = None, None
    for i, t in enumerate(times):
        diff = abs((datetime.fromisoformat(t) - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = i, diff
    # The response covers the flight's day; more than an hour off means the hour is missing.
    if best is None or best_diff > 3600:
        return None
    record: dict[str, Any] = {"time": datetime.fromisoformat(times[best]).replace(tzinfo=UTC).isoformat()}
    for var, key in HOURLY.items():
        values = hourly.get(var) or []
        record[key] = values[best] if best < len(values) else None
    if record["wind_ms"] is None and record["temp_c"] is None:
        return None
    return record


async def async_fetch_day(
    session: aiohttp.ClientSession, lat: float, lon: float, day: date
) -> dict[str, list[Any]]:
    """Hourly values (UTC) for one place and day."""
    params = {
        "latitude": f"{lat:.2f}",
        "longitude": f"{lon:.2f}",
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
        "hourly": ",".join(HOURLY),
        "wind_speed_unit": "ms",
        "timezone": "GMT",
    }
    try:
        async with session.get(ARCHIVE_URL, params=params, timeout=_TIMEOUT) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = {}
            if resp.status == 400:
                # E.g. a day the archive does not reach yet.
                raise WeatherUnavailable(str(body.get("reason") or "bad request"))
            if resp.status != 200:
                raise WeatherError(f"HTTP {resp.status}: {body.get('reason') or ''}".strip())
    except (aiohttp.ClientError, TimeoutError) as err:
        raise WeatherError(str(err) or type(err).__name__) from err
    hourly = body.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherUnavailable("no hourly data")
    return hourly


async def async_weather_for_flights(
    session: aiohttp.ClientSession, flights: list[dict[str, Any]], *, max_requests: int
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Look up the weather for several flights.

    Flights at the same (rounded) place on the same day share one request.
    Returns ``(weather by flight id, ids to retry later)``; flights not in
    either had no position. Stops early after ``max_requests`` requests or
    when Open-Meteo fails; the flights not reached are simply left out.
    """
    groups: dict[tuple[float, float, date], list[tuple[dict[str, Any], datetime]]] = {}
    for f in flights:
        pos = flight_position(f)
        if pos is None:
            continue
        moment = flight_moment(f)
        groups.setdefault((*pos, moment.date()), []).append((f, moment))

    found: dict[str, dict[str, Any]] = {}
    retry: set[str] = set()
    fetched_at = datetime.now(UTC).isoformat()
    for requests, ((lat, lon, day), members) in enumerate(groups.items()):
        if requests >= max_requests:
            break
        try:
            hourly = await async_fetch_day(session, lat, lon, day)
        except WeatherUnavailable as err:
            _LOGGER.debug("No weather for %s,%s on %s: %s", lat, lon, day, err)
            retry.update(f["flight_id"] for f, _ in members)
            continue
        except WeatherError as err:
            _LOGGER.warning("Open-Meteo weather lookup failed, retrying on a later scan: %s", err)
            break
        for f, moment in members:
            record = pick_hour(hourly, moment)
            if record is None:
                retry.add(f["flight_id"])
            else:
                found[f["flight_id"]] = {**record, "source": SOURCE, "fetched_at": fetched_at}
    return found, retry

"""Recordings (videos/photos) and their link to flights.

Pure functions only (no Home Assistant, no network) so they are easy to test:

* :func:`classify_name` tells what a file is from its name alone.
* :func:`build_recordings` groups the files of one shot (original, proxy,
  equirectangular render, cover image, raw, subtitle telemetry) into one
  *recording*.
* :func:`match_recordings` assigns recordings to flights by time.

Naming conventions handled (DJI cameras write the local time of the
recording start into the name)::

    DJI_20260921190306_0001_D.OSV        360° original (Avata 360: two fisheye streams)
    DJI_20260921190306_0001_D.LRF        low-res proxy (MP4 container, HEVC)
    DJI_20260921190306_0001_D_proxy.mp4  proxy renamed by the PC sync script
    DJI_20260921190306_0001_D_360.mp4    360° proxy stitched to equirect (H.264) by the sync script
    DJI_20260921190306_0001_D_cover.jpg  cover frame extracted by the PC sync script
    DJI_20260921190306_0001_D.MP4        normal video
    DJI_20260921190306_0001_D.JPG/.DNG   photo / raw photo
    DJI_20260921190306_0001_D.SRT        per-frame telemetry subtitles

Anything else is matched by a ``YYYYMMDD_HHMMSS``-like timestamp in the
name (e.g. clips DJI Fly saved to the phone gallery) or by the capture time
OneDrive extracted from the file.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import PurePosixPath
from typing import Any

ROLE_ORIGINAL = "original"
ROLE_PROXY = "proxy"
ROLE_EQUIRECT = "equirect"  # 360° proxy stitched to an equirectangular video
ROLE_COVER = "cover"
ROLE_RAW = "raw"
ROLE_SRT = "srt"

_ROLES = (ROLE_ORIGINAL, ROLE_PROXY, ROLE_EQUIRECT, ROLE_COVER, ROLE_RAW, ROLE_SRT)

KIND_VIDEO = "video"
KIND_360 = "360"
KIND_PHOTO = "photo"

# How the browser has to project the played file (see media_coordinator.play_projection).
PROJECTION_EQUIRECT = "equirect"
PROJECTION_DFISHEYE = "dfisheye"  # two fisheye circles side by side (the raw 360° proxy)

_EXT_VIDEO = {".mp4", ".mov"}
_EXT_360 = {".osv", ".360", ".insv"}
_EXT_PHOTO = {".jpg", ".jpeg", ".heic", ".png"}
_EXT_RAW = {".dng"}
_EXT_PROXY = {".lrf"}
_EXT_SRT = {".srt"}
MEDIA_EXTENSIONS = _EXT_VIDEO | _EXT_360 | _EXT_PHOTO | _EXT_RAW | _EXT_PROXY | _EXT_SRT

# DJI_<yyyymmddHHMMSS>_<nnnn>[_<lens/type letter>][_proxy|_cover|_360]
_DJI_NAME = re.compile(
    r"^(?P<key>DJI_(?P<ts>\d{14})_(?P<seq>\d{3,4})(?:_[A-Z])?)(?:_(?P<suffix>proxy|cover|360))?$",
    re.IGNORECASE,
)
_SUFFIX = re.compile(r"^(?P<key>.+?)[_-](?P<suffix>proxy|cover|360)$", re.IGNORECASE)
# 20260921_190306, 2026-09-21 19.03.06, 20260921190306, ...
_GENERIC_TS = re.compile(
    r"(?<!\d)(?P<y>20\d{2})[-_.]?(?P<mo>\d{2})[-_.]?(?P<d>\d{2})[-_. T]?"
    r"(?P<h>\d{2})[-_.:]?(?P<mi>\d{2})[-_.:]?(?P<s>\d{2})(?!\d)"
)


@dataclass(frozen=True)
class NameInfo:
    """What the file name says about a media file."""

    key: str  # files with the same key belong to the same recording
    role: str
    kind: str | None  # None for sidecars that do not decide the kind
    local_time: datetime | None  # naive, camera/phone local time


def _parse_local(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def classify_name(name: str) -> NameInfo | None:
    """Return the role of a media file, or ``None`` if it is not media."""
    path = PurePosixPath(name)
    ext = path.suffix.lower()
    if ext not in MEDIA_EXTENSIONS:
        return None
    stem = path.stem

    suffix = None
    local_time = None
    m = _DJI_NAME.match(stem)
    if m:
        key = m["key"].upper()
        suffix = (m["suffix"] or "").lower() or None
        local_time = _parse_local(m["ts"])
    else:
        s = _SUFFIX.match(stem)
        if s:
            stem, suffix = s["key"], s["suffix"].lower()
        key = stem
        g = _GENERIC_TS.search(stem)
        if g:
            local_time = _parse_local("".join(g.group("y", "mo", "d", "h", "mi", "s")))

    if suffix == "cover" and ext in _EXT_PHOTO:
        return NameInfo(key, ROLE_COVER, None, local_time)
    if ext in _EXT_PROXY or (suffix == "proxy" and ext in _EXT_VIDEO):
        return NameInfo(key, ROLE_PROXY, None, local_time)
    if suffix == "360" and ext in _EXT_VIDEO:
        # No kind: next to an .OSV it changes nothing, without one the
        # recording still becomes 360° (see build_recordings).
        return NameInfo(key, ROLE_EQUIRECT, None, local_time)
    if ext in _EXT_SRT:
        return NameInfo(key, ROLE_SRT, None, local_time)
    if ext in _EXT_RAW:
        return NameInfo(key, ROLE_RAW, KIND_PHOTO, local_time)
    if ext in _EXT_360:
        return NameInfo(key, ROLE_ORIGINAL, KIND_360, local_time)
    if ext in _EXT_VIDEO:
        return NameInfo(key, ROLE_ORIGINAL, KIND_VIDEO, local_time)
    return NameInfo(key, ROLE_ORIGINAL, KIND_PHOTO, local_time)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _file_ref(item: dict[str, Any]) -> dict[str, Any]:
    """The part of a stored item needed to show or fetch the file."""
    ref = {
        "item_id": item["id"],
        "name": item["name"],
        "size": item.get("size"),
        "web_url": item.get("web_url"),
    }
    if item.get("path"):
        ref["path"] = item["path"]  # local folder: relative to the folder
    return ref


def build_recordings(items: dict[str, dict[str, Any]], tz: tzinfo) -> dict[str, dict[str, Any]]:
    """Group stored items into recordings.

    ``items`` maps item id to ``{"id", "name", "size", "web_url", "folder",
    "taken_at", "duration_ms", "width", "height"}`` (see ``onedrive.py``;
    ``local_media.py`` adds ``path`` and ``mtime``).
    ``tz`` is the zone the camera clock runs in (Home Assistant's zone).
    Files are grouped per folder, so a re-used DJI counter in another folder
    never merges two shots.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items.values():
        info = classify_name(item["name"])
        if info is None:
            continue
        gkey = (item.get("folder") or "", info.key.lower())
        group = groups.setdefault(gkey, {"files": {}, "kind": None, "local_time": None, "taken_at": None})
        # Two originals with the same key (e.g. .MP4 and .JPG of one shot) are
        # rare; keep the video, it is the more interesting one.
        existing = group["files"].get(info.role)
        if existing is None or (info.kind in (KIND_VIDEO, KIND_360) and group["kind"] == KIND_PHOTO):
            group["files"][info.role] = item
        if info.kind and (group["kind"] in (None, KIND_PHOTO) or info.kind == KIND_360):
            group["kind"] = info.kind
        group["local_time"] = group["local_time"] or info.local_time
        group["taken_at"] = group["taken_at"] or item.get("taken_at")

    recordings: dict[str, dict[str, Any]] = {}
    for (_folder, _key), group in groups.items():
        files = group["files"]
        main = _main_file(files)
        if main is None:
            continue  # only a cover or subtitles
        # Proxy only: a stitched 360° render tells what it was.
        kind = group["kind"] or (KIND_360 if ROLE_EQUIRECT in files else KIND_VIDEO)

        if group["local_time"] is not None:
            start = group["local_time"].replace(tzinfo=tz)
        elif group["taken_at"]:
            # OneDrive's photo.takenDateTime is the EXIF time, which carries
            # no zone; Graph still suffixes it with "Z". Treat it as local.
            parsed = _parse_iso(group["taken_at"])
            start = parsed.replace(tzinfo=tz) if parsed else None
        else:
            start = None

        duration_ms = next(
            (
                f.get("duration_ms")
                for f in (files.get(ROLE_ORIGINAL), files.get(ROLE_PROXY), files.get(ROLE_EQUIRECT))
                if f and f.get("duration_ms")
            ),
            None,
        )
        rec_id = main["id"]
        recordings[rec_id] = {
            "id": rec_id,
            "name": main["name"],
            "kind": kind,
            "start": start.astimezone(UTC).isoformat() if start else None,
            "duration_s": round(duration_ms / 1000, 1) if duration_ms else None,
            "web_url": main.get("web_url"),
            "folder": main.get("folder"),
            **{role: _file_ref(f) for role, f in files.items()},
        }
    return recordings


def _main_file(files: dict[str, Any]) -> dict[str, Any] | None:
    """The file that names a recording (its id, name and size come from it)."""
    for role in (ROLE_ORIGINAL, ROLE_RAW, ROLE_PROXY, ROLE_EQUIRECT):
        if files.get(role):
            return files[role]
    return None


def match_recordings(
    flights: list[dict[str, Any]] | Any,
    recordings: list[dict[str, Any]] | Any,
    tolerance_s: float = 120,
) -> dict[str, list[str]]:
    """Assign each recording to at most one flight.

    A recording belongs to a flight when its time span overlaps the flight
    (widened by ``tolerance_s`` on both sides, for clock drift and for
    recordings started just before take-off). With several candidates the
    flight with the smallest gap, then the largest overlap wins.

    Returns ``{flight_id: [recording ids sorted by start]}``.
    """
    tol = timedelta(seconds=tolerance_s)
    windows: list[tuple[datetime, datetime, str]] = []
    for f in flights:
        start = _parse_iso(f.get("start_time"))
        if start is None:
            continue
        end = _parse_iso(f.get("end_time")) or start + timedelta(seconds=float(f.get("duration_s") or 0))
        windows.append((start, max(end, start), f["flight_id"]))
    windows.sort()
    starts = [w[0] for w in windows]
    longest = max((fe - fs for fs, fe, _ in windows), default=timedelta(0))

    assigned: dict[str, list[tuple[datetime, str]]] = {}
    for rec in recordings:
        rs = _parse_iso(rec.get("start"))
        if rs is None:
            continue
        re_ = rs + timedelta(seconds=float(rec.get("duration_s") or 0))
        best: tuple[tuple[float, float], str] | None = None
        # Only flights starting in [rs - tol - longest, re_ + tol] can overlap.
        lo = bisect_left(starts, rs - tol - longest)
        hi = bisect_right(starts, re_ + tol)
        for fs, fe, fid in windows[lo:hi]:
            if rs > fe + tol or re_ < fs - tol:
                continue
            overlap = (min(re_, fe) - max(rs, fs)).total_seconds()
            score = (max(0.0, -overlap), -overlap)
            if best is None or score < best[0]:
                best = (score, fid)
        if best is not None:
            assigned.setdefault(best[1], []).append((rs, rec["id"]))

    return {fid: [rid for _t, rid in sorted(recs)] for fid, recs in assigned.items()}


# DJI Fly: FlightRecord_2026-09-21_[18-58-21].txt, older apps DJIFlightRecord_....txt.
# Strict on purpose: only these are copied into the log folder, under this name.
_FLIGHT_RECORD = re.compile(r"^(?:DJI)?FlightRecord_[\w\-\[\]() .]{1,100}\.txt$", re.IGNORECASE)


def is_flight_record(name: str) -> bool:
    """A DJI Fly flight record (to import), as opposed to a recording."""
    return bool(_FLIGHT_RECORD.match(name))


def _dedupe_key(rec: dict[str, Any]) -> tuple[str, int] | None:
    main = _main_file(rec) or {}
    size = main.get("size")
    return (rec["name"].lower(), size) if size else None


def duplicate_recordings(sources: list[dict[str, dict[str, Any]]]) -> set[str]:
    """Ids of recordings that another source holds as well.

    The same shot copied to two places (say OneDrive and the NAS) has the
    same file name and size. Of such copies the one with the most files
    (proxy, cover, raw, ...) stays; on a tie the one of the earlier source.
    """
    kept: dict[tuple[str, int], dict[str, Any]] = {}
    duplicates: set[str] = set()
    for recordings in sources:
        for rec in recordings.values():
            key = _dedupe_key(rec)
            if key is None:
                continue
            other = kept.get(key)
            if other is None:
                kept[key] = rec
            elif _richness(rec) > _richness(other):
                duplicates.add(other["id"])
                kept[key] = rec
            else:
                duplicates.add(rec["id"])
    return duplicates


def _richness(rec: dict[str, Any]) -> int:
    return sum(1 for role in _ROLES if rec.get(role))

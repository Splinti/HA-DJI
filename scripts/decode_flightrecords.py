"""Decrypt DJI Fly flight records and dump everything pydjirecord can read.

For each ``*.txt`` in the input folder this writes to ``<input>/decoded/``:

* ``<name>.keychains.json`` - the AES keys DJI returned for this log. With
  them the log can be decoded again later without an API key.
* ``<name>.frames.json``    - details (incl. anomaly / rc_signal) + all frames
* ``<name>.records.json``   - the raw records (firmware, warnings, params, ...)

Usage (PowerShell, from the repo root)::

    $env:DJI_API_KEY = "..."; C:\\tmp\\djiha\\venv\\Scripts\\python.exe scripts\\decode_flightrecords.py

Optional argument: the log folder (default ``./flightrecords``). If the logs
were decoded before, the saved keychains are used and no key is needed.
The output contains serial numbers and GPS positions - keep it out of git
(``flightrecords/`` is gitignored).
"""

from __future__ import annotations

import collections
import dataclasses
import json
import os
import sys
from pathlib import Path

from pydjirecord import DJILog
from pydjirecord.export.json import _dataclass_to_dict
from pydjirecord.frame.details import FrameDetails
from pydjirecord.keychain.api import KeychainFeaturePoint


def _api_key() -> str:
    return os.environ.get("DJI_API_KEY", "").strip()


def _keychains(log: DJILog, cache: Path, key: str | None) -> list[list[KeychainFeaturePoint]]:
    if log.version < 13:
        return []
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
        return [[KeychainFeaturePoint(**fp) for fp in group] for group in raw]
    if not key:
        raise RuntimeError("encrypted log and no DJI_API_KEY set")
    keychains = log.fetch_keychains(key)
    cache.write_text(
        json.dumps([[dataclasses.asdict(fp) for fp in group] for group in keychains]),
        encoding="utf-8",
    )
    return keychains


def _dump(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, separators=(",", ":"), default=str), encoding="utf-8")


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "flightrecords")
    files = sorted(p for p in src.glob("*.txt") if p.is_file())
    if not files:
        print(f"No *.txt logs in {src.resolve()}")
        return 1
    out = src / "decoded"
    out.mkdir(exist_ok=True)

    key: str | None = None
    failed = 0
    for path in files:
        try:
            log = DJILog.from_bytes(path.read_bytes())
            cache = out / f"{path.stem}.keychains.json"
            if log.version >= 13 and not cache.exists() and key is None:
                key = _api_key()
            keychains = _keychains(log, cache, key)

            records = log.records(keychains)
            frames = log.frames(keychains)
            details = FrameDetails.from_details(log.details, frames)

            _dump(
                out / f"{path.stem}.frames.json",
                {
                    "version": log.version,
                    "header": _dataclass_to_dict(log.details),
                    "details": _dataclass_to_dict(details),
                    "frames": [_dataclass_to_dict(f) for f in frames],
                },
            )
            types = collections.Counter(f"{rec.record_type}:{type(rec.data).__name__}" for rec in records)
            _dump(
                out / f"{path.stem}.records.json",
                {
                    "version": log.version,
                    "record_types": dict(types.most_common()),
                    "records": [
                        {
                            "type": rec.record_type,
                            "class": type(rec.data).__name__,
                            "content": _dataclass_to_dict(rec.data),
                        }
                        for rec in records
                        # 50/56 are the key storage records; bytes are unparsed types
                        if rec.record_type not in (50, 56) and not isinstance(rec.data, (bytes, bytearray))
                    ],
                },
            )
            print(f"OK    {path.name}: {len(frames)} frames, {len(records)} records")
        except Exception as err:  # keep going, report at the end
            failed += 1
            print(f"FAIL  {path.name}: {type(err).__name__}: {err}")

    print(f"\n{len(files) - failed}/{len(files)} decoded -> {out.resolve()}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

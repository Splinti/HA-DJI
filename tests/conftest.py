"""Shared fixtures."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


if sys.platform == "win32":
    # HA's test harness blocks socket creation, but the Windows event loop
    # needs an AF_INET socketpair for its self-pipe. Neutralise the block on
    # Windows only; the harness still refuses connections to real hosts.
    import pytest_socket

    pytest_socket.disable_socket = lambda allow_unix_socket=False: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ for every test."""
    yield


# --- minimal stand-ins for pydjirecord frames -------------------------------


@dataclass
class OSD:
    fly_time: float = 0.0
    latitude: float = 0.0
    longitude: float = 0.0
    height: float = 0.0
    altitude: float = 0.0
    h_speed: float = 0.0
    z_speed: float = 0.0
    cumulative_distance: float = 0.0
    gps_level: int = 5
    flight_action: str | None = None
    flyc_state: str | None = None
    is_motor_blocked: bool = False


@dataclass
class Battery:
    charge_level: int = 0
    # Smart battery readings; voltage 0 means "no battery record yet".
    voltage: float = 0.0
    temperature: float = 0.0
    design_capacity: int = 0
    full_capacity: int = 0
    number_of_discharges: int = 0
    lifetime_remaining: int = 0
    cell_voltages: list[float] = field(default_factory=list)
    is_cell_voltage_estimated: bool = True
    cell_voltage_deviation: float = 0.0


@dataclass
class Recover:
    battery_sn: str = ""


@dataclass
class Home:
    latitude: float = 0.0
    longitude: float = 0.0


@dataclass
class Camera:
    is_video: bool = False
    record_time: int = 0
    remain_photo_num: int = 0


@dataclass
class Custom:
    date_time: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))


@dataclass
class Frame:
    osd: OSD = field(default_factory=OSD)
    battery: Battery = field(default_factory=Battery)
    home: Home = field(default_factory=Home)
    custom: Custom = field(default_factory=Custom)
    camera: Camera = field(default_factory=Camera)
    recover: Recover = field(default_factory=Recover)


def make_frames(n: int = 100, *, start: datetime | None = None) -> list[Frame]:
    """A straight north-bound flight: 1 frame/s, 10 m/s, climbing to 60 m."""
    start = start or datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    frames = []
    for i in range(n):
        frames.append(
            Frame(
                osd=OSD(
                    fly_time=float(i),
                    latitude=48.1 + i * 0.0001,
                    longitude=11.5,
                    height=min(60.0, i * 1.0),
                    altitude=500.0 + min(60.0, i * 1.0),
                    h_speed=10.0 if 0 < i < n - 1 else 0.0,
                    z_speed=1.0 if i < 60 else 0.0,
                    cumulative_distance=i * 10.0,
                    gps_level=5,
                ),
                battery=Battery(charge_level=95 - i // 4),
                home=Home(latitude=48.1, longitude=11.5),
                custom=Custom(date_time=start + timedelta(seconds=i)),
            )
        )
    return frames


@pytest.fixture
def frames() -> list[Frame]:
    return make_frames()

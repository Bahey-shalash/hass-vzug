"""Zones and programs, as appliances report them through 'getProgram'.

Refrigerators answer 'getProgram' with one entry per zone, dishwashers with the
selected or running program (or just {"status":"idle"}).

Everything here is plain data in, plain data out, so it can be tested without
Home Assistant. The coordinator in 'shared.py' fills a 'ZoneState', the entities
only read from it and write through 'api.VZugApi.set_zone_value' /
'api.VZugApi.set_cmd_value'.
"""

import dataclasses
import re
from collections.abc import Iterable, Mapping
from typing import Any

# functions that run for a while and then switch themselves off. The value is the
# duration in seconds, "0" stops them early.
BOOST_KEYS: tuple[str, ...] = ("superCool", "superFreeze")
# PartyCooling covers the whole appliance: setting it on one zone sets every zone.
PARTY_KEY = "partyCooling"
TEMP_KEY = "temp"
DOOR_KEY = "doorClosed"
# the kitchen timer is a zone of its own ('countdown1'), its value is 'duration'
TIMER_KEY = "duration"

# 'cmd' settings outside the category tree, read with 'getcmd<name>'
SWITCH_MODES: tuple[str, ...] = ("HolidayMode", "SilentPlusMode")
SENSOR_MODES: tuple[str, ...] = ("SabbatMode",)
UNIT_MODE = "TemperatureUnit"
ALL_MODES: tuple[str, ...] = (*SWITCH_MODES, *SENSOR_MODES, UNIT_MODE)

Zone = Mapping[str, Any]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ZoneState:
    zones: Mapping[str, Zone]
    """'getProgram' entries keyed by their zone name"""
    modes: Mapping[str, str]
    """raw answers to 'getcmd<name>' for the modes this appliance supports"""


def index_zones(raw_programs: Iterable[Mapping[str, Any]]) -> dict[str, Zone]:
    """Key the 'getProgram' entries by zone name, dropping entries without one."""
    return {
        program["zone"]: program
        for program in raw_programs
        if isinstance(program.get("zone"), str)
    }


def has_zones(raw_programs: Iterable[Mapping[str, Any]]) -> bool:
    return bool(index_zones(raw_programs))


def zones_with(zones: Mapping[str, Zone], key: str) -> list[str]:
    """Names of the zones that offer 'key', in the order the appliance lists them."""
    return [name for name, zone in zones.items() if isinstance(zone.get(key), Mapping)]


def is_timer_zone(name: str) -> bool:
    return name.startswith("countdown")


def value_field(zone: Zone | None, key: str, field: str) -> Any:
    """ex. value_field(zone, "temp", "set") -> 6.0, None when missing."""
    if not zone:
        return None
    value = zone.get(key)
    if not isinstance(value, Mapping):
        return None
    return value.get(field)


def is_running(zone: Zone | None, key: str) -> bool:
    """A timed function runs while the appliance reports how long it was set for."""
    seconds = value_field(zone, key, "set")
    return isinstance(seconds, (int, float)) and seconds > 0


def remaining_seconds(zone: Zone | None, key: str) -> int | None:
    if not is_running(zone, key):
        return None
    left = value_field(zone, key, "act")
    if not isinstance(left, (int, float)):
        return None
    return max(int(left), 0)


def format_value(value: float) -> str:
    """The appliance wants strings, and integral values without a trailing '.0'."""
    return str(int(value)) if float(value).is_integer() else str(value)


def mode_is_on(raw: str | None) -> bool | None:
    """'true' / 'false' for the switches, a number of hours ('0' = off) for Sabbath."""
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return float(lowered) > 0
    except ValueError:
        return None


def temperature_unit(modes: Mapping[str, str]) -> str:
    return "°F" if "F" in modes.get(UNIT_MODE, "") else "°C"


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ProgramState:
    names: Mapping[int, str]
    """program id -> name, in the order the appliance lists them"""
    current: Mapping[str, Any] | None
    """the selected or running program, None while idle"""
    total_seconds: int | None = None
    """full length of the running program, as far as we know it (see 'track_run')"""


def current_program(
    raw_programs: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for program in raw_programs:
        if isinstance(program.get("id"), int) and program.get("status") != "idle":
            return program
    return None


def program_key(name: str) -> str:
    """'Fondue/Raclette' -> 'fondue_raclette', the select option for a program.

    Options are translation keys so Home Assistant can show each program with its
    own name and icon. A program we have no translation for shows its key.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def program_id_by_key(names: Mapping[int, str], key: str) -> int | None:
    return next((pid for pid, name in names.items() if program_key(name) == key), None)


def program_options(program: Mapping[str, Any]) -> dict[str, bool]:
    """The on/off options of a selected program, as 'Send to appliance' sends them.

    Recorded from the V-ZUG app for Eco:
    {"id":50,"dryPlus":false,"energySaving":false,"partialload":false,"rinsePlus":false}
    Only options the appliance lets you change (those listing 'options') are sent.
    """
    return {
        key: bool(value.get("set", False))
        for key, value in program.items()
        if isinstance(value, Mapping) and value.get("options") == [True, False]
    }


@dataclasses.dataclass(frozen=True, slots=True)
class Run:
    """The program that is running and how long it runs in total."""

    program_id: int
    total_seconds: int


def run_seconds_left(program: Mapping[str, Any] | None) -> int | None:
    """Seconds left of a running program, from 'duration.act'."""
    if not program or program.get("status") != "active":
        return None
    left = value_field(program, "duration", "act")
    return max(int(left), 0) if isinstance(left, (int, float)) else None


def track_run(
    previous: Run | None,
    previous_program: Mapping[str, Any] | None,
    program: Mapping[str, Any] | None,
) -> Run | None:
    """Work out the full length of the running program.

    While running the appliance only reports the time left ('duration.act'). Its
    length is known beforehand only while the program is selected
    ('duration.set'), so remember that when the run starts. If we only see the run
    once it is going (Home Assistant started mid-run), the first time left we see
    is the best we have, and progress counts from there.
    """
    left = run_seconds_left(program)
    if left is None:
        return None
    program_id = program.get("id")
    if previous is not None and previous.program_id == program_id:
        # the appliance can add time, ex. when the water heats slower
        return Run(program_id, max(previous.total_seconds, left))
    planned = value_field(previous_program, "duration", "set")
    if (
        previous_program is not None
        and previous_program.get("id") == program_id
        and isinstance(planned, (int, float))
        and planned >= left
    ):
        return Run(program_id, int(planned))
    return Run(program_id, left)


def progress_percent(total: int | None, left: int | None) -> int | None:
    if not total or left is None:
        return None
    return min(100, max(0, round(100 * (total - left) / total)))

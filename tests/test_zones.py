"""Zone handling for refrigerators, based on a CombiCooler V2000 (API 1.8.0)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.vzug import zones
from custom_components.vzug.api import VZugApi

# 'getProgram' of a CombiCooler V2000 with SuperFreeze and the timer running
COMBI_COOLER = [
    {
        "id": 2000,
        "status": "active",
        "temp": {"set": 6.0, "act": 6.0, "min": 3.0, "max": 8.0},
        "superCool": {"min": 0, "max": 86400},
        "partyCooling": {"min": 0, "max": 172800},
        "doorClosed": True,
        "zone": "fridge1",
    },
    {
        "id": 2001,
        "status": "active",
        "temp": {"set": -18.0, "act": -18.0, "min": -22.0, "max": -14.0},
        "superFreeze": {"set": 3600, "act": 3596, "min": 0, "max": 194400},
        "partyCooling": {"min": 0, "max": 172800},
        "doorClosed": False,
        "zone": "freezer1",
    },
    {
        "id": 9000,
        "status": "active",
        "duration": {"set": 600, "act": 597, "min": 0, "max": 35700},
        "zone": "countdown1",
    },
]

# 'getProgram' of an idle dishwasher
DISHWASHER = [{"status": "idle"}]


def test_index_zones_keys_by_zone_name():
    indexed = zones.index_zones(COMBI_COOLER)
    assert list(indexed) == ["fridge1", "freezer1", "countdown1"]
    assert indexed["freezer1"]["id"] == 2001


def test_dishwasher_has_no_zones():
    assert not zones.has_zones(DISHWASHER)
    assert zones.has_zones(COMBI_COOLER)


def test_zones_with_finds_the_zones_offering_a_key():
    indexed = zones.index_zones(COMBI_COOLER)
    assert zones.zones_with(indexed, "temp") == ["fridge1", "freezer1"]
    assert zones.zones_with(indexed, "superCool") == ["fridge1"]
    assert zones.zones_with(indexed, "superFreeze") == ["freezer1"]
    assert zones.zones_with(indexed, "partyCooling") == ["fridge1", "freezer1"]
    # 'doorClosed' is a plain bool, not a value object
    assert zones.zones_with(indexed, "doorClosed") == []


def test_running_and_remaining():
    indexed = zones.index_zones(COMBI_COOLER)
    assert zones.is_running(indexed["freezer1"], "superFreeze")
    assert zones.remaining_seconds(indexed["freezer1"], "superFreeze") == 3596
    assert not zones.is_running(indexed["fridge1"], "superCool")
    assert zones.remaining_seconds(indexed["fridge1"], "superCool") is None
    assert zones.remaining_seconds(indexed["countdown1"], "duration") == 597
    assert zones.remaining_seconds(None, "duration") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(7, "7"), (7.0, "7"), (-18.0, "-18"), (6.5, "6.5"), (0, "0")],
)
def test_format_value(value, expected):
    assert zones.format_value(value) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("false", False),
        ("0", False),
        ("30", True),
        (None, None),
        ("?", None),
    ],
)
def test_mode_is_on(raw, expected):
    assert zones.mode_is_on(raw) is expected


def test_temperature_unit():
    assert zones.temperature_unit({"TemperatureUnit": "°C"}) == "°C"
    assert zones.temperature_unit({"TemperatureUnit": "°F"}) == "°F"
    assert zones.temperature_unit({}) == "°C"


@pytest.mark.asyncio
async def test_set_zone_value_sends_what_the_app_sends():
    """Recorded from the V-ZUG app: zone by name, value as a string."""
    api = VZugApi(base_url="http://example.com")
    with patch.object(api, "_command", new_callable=AsyncMock) as command:
        await api.set_zone_value("fridge1", "partyCooling", "43200")
    command.assert_called_once()
    args, kwargs = command.call_args
    assert args == ("hh",)
    assert kwargs["command"] == "setProgram"
    # byte for byte what the app sends, the appliance refuses JSON with spaces
    assert kwargs["params"]["value"] == '{"zone":"fridge1","partyCooling":"43200"}'


@pytest.mark.asyncio
async def test_set_program_sends_compact_json():
    api = VZugApi(base_url="http://example.com")
    with patch.object(api, "_command", new_callable=AsyncMock) as command:
        await api.set_program(50, {"dryPlus": False, "rinsePlus": False})
    assert command.call_args.kwargs["params"]["value"] == (
        '{"dryPlus":false,"rinsePlus":false,"id":50}'
    )


@pytest.mark.asyncio
async def test_cmd_values_use_getcmd_and_setcmd():
    api = VZugApi(base_url="http://example.com")
    with patch.object(api, "_command", new_callable=AsyncMock) as command:
        command.return_value = "false\n"
        assert await api.get_cmd_value("HolidayMode") == "false"
        assert command.call_args.kwargs["command"] == "getcmdHolidayMode"

        await api.set_cmd_value("SilentPlusMode", "true")
        assert command.call_args.kwargs["command"] == "setcmdSilentPlusMode"
        assert command.call_args.kwargs["params"] == {"value": "true"}


def test_program_keys_have_translations_and_icons():
    """Every program the AdoraDish V4000 reports must have a name and an icon."""
    import pathlib

    base = pathlib.Path(zones.__file__).parent
    icons = json.loads((base / "icons.json").read_text())
    icon_states = icons["entity"]["select"]["program_select"]["state"]
    names = [
        "Eco",
        "Automatic",
        "Quick",
        "Intensive",
        "Glass",
        "Fondue/Raclette",
        "Hygiene",
        "Pre-rinsing",
    ]
    for lang in ("en", "de", "fr"):
        translations = json.loads((base / "translations" / f"{lang}.json").read_text())
        states = translations["entity"]["select"]["program_select"]["state"]
        for name in names:
            assert zones.program_key(name) in states, (lang, name)
    for name in names:
        assert zones.program_key(name) in icon_states, name
    assert zones.program_key("Fondue/Raclette") == "fondue_raclette"
    assert zones.program_key("Pre-rinsing") == "pre_rinsing"


# AdoraDish V4000, recorded: Eco selected, then Intensive running
ECO_SELECTED = {
    "id": 50,
    "name": "Eco",
    "status": "selected",
    "duration": {"set": 21300},
}
INTENSIVE_RUNNING = {
    "id": 54,
    "name": "Intensive",
    "status": "active",
    "duration": {"act": 3999},
}


def test_run_seconds_left():
    assert zones.run_seconds_left(INTENSIVE_RUNNING) == 3999
    assert zones.run_seconds_left(ECO_SELECTED) is None
    assert zones.run_seconds_left(None) is None


def test_track_run_uses_the_planned_length_when_seen_selected():
    eco_running = {"id": 50, "status": "active", "duration": {"act": 21000}}
    run = zones.track_run(None, ECO_SELECTED, eco_running)
    assert run == zones.Run(50, 21300)
    assert zones.progress_percent(run.total_seconds, 21000) == 1


def test_track_run_started_mid_run_counts_from_first_sight():
    run = zones.track_run(None, None, INTENSIVE_RUNNING)
    assert run == zones.Run(54, 3999)
    later = {**INTENSIVE_RUNNING, "duration": {"act": 2000}}
    run = zones.track_run(run, INTENSIVE_RUNNING, later)
    assert run == zones.Run(54, 3999)
    assert zones.progress_percent(run.total_seconds, 2000) == 50


def test_track_run_grows_when_the_appliance_adds_time():
    run = zones.Run(54, 3000)
    assert zones.track_run(run, INTENSIVE_RUNNING, INTENSIVE_RUNNING) == zones.Run(
        54, 3999
    )


def test_track_run_ends_when_idle():
    assert zones.track_run(zones.Run(54, 3999), INTENSIVE_RUNNING, None) is None


def test_track_run_ignores_a_selection_of_another_program():
    run = zones.track_run(None, ECO_SELECTED, INTENSIVE_RUNNING)
    assert run == zones.Run(54, 3999)


@pytest.mark.parametrize(
    ("total", "left", "expected"),
    [
        (100, 100, 0),
        (100, 25, 75),
        (100, 0, 100),
        (100, 150, 0),
        (None, 10, None),
        (100, None, None),
    ],
)
def test_progress_percent(total, left, expected):
    assert zones.progress_percent(total, left) == expected

import json
from pathlib import Path

import pytest

from driver_autoterm.protocol import MSG_INIT, MSG_SERIAL, MSG_SETTINGS, MSG_STATUS, MSG_VERSION

SCHEMA = Path(__file__).resolve().parents[2] / "hub" / "schema" / "heater.schema.json"


def test_connect_sync_order_is_init_then_settings_then_telemetry(make_rig):
    rig = make_rig()
    rig.poll()
    order = [m for m, _ in rig.heater.received]
    assert order[:5] == [MSG_INIT, MSG_SERIAL, MSG_VERSION, MSG_SETTINGS, MSG_STATUS]


def test_init_can_be_turned_off(make_rig):
    rig = make_rig(send_init=False)
    rig.poll()
    assert [m for m, _ in rig.heater.received][:2] == [MSG_SETTINGS, MSG_STATUS]


def test_publishes_identity_and_idle_telemetry(rig):
    assert rig.value("driver") == "autoterm"
    assert rig.value("name") == "Heater"
    assert rig.value("commands_enabled") is True
    assert rig.value("phase") == "off"
    assert rig.value("status_code") == "0.1"
    assert rig.value("error_code") == 0
    assert rig.value("internal_temperature_c") == 19
    assert rig.value("voltage_v") == 13.4
    assert rig.value("heater_temperature_c") == 21
    assert rig.value("external_temperature_c") is None  # 0x7F sentinel -> empty retained
    assert rig.value("ventilating") is None


def test_settings_come_from_the_heater_not_from_defaults(rig):
    assert (rig.value("mode"), rig.value("setpoint_c"), rig.value("power_level")) == (
        "power",
        16,
        8,
    )


def test_empty_name_is_not_published(make_rig):
    rig = make_rig(name="")
    assert "renewvan/heater/air2d/name" not in rig.pub.retained


@pytest.mark.parametrize(
    "code,phase",
    [
        ((0, 1), "off"),
        ((0, 0), "off"),
        ((1, 0), "starting"),
        ((1, 1), "starting"),
        ((2, 0), "starting"),
        ((2, 1), "starting"),
        ((2, 2), "starting"),
        ((2, 6), "starting"),
        ((2, 3), "starting"),
        ((2, 4), "warming_up"),
        ((3, 0), "running"),
        ((3, 35), "running"),
        ((3, 4), "standby"),
        ((3, 5), "standby"),
        ((4, 0), "shutting_down"),
    ],
)
def test_phase_mapping(rig, code, phase):
    rig.set_status(*code)
    rig.poll()
    assert rig.value("phase") == phase
    assert rig.value("status_code") == f"{code[0]}.{code[1]}"
    assert rig.value("ventilating") == (True if code == (3, 35) else None)


def test_captured_cycle_never_publishes_off_between_start_and_shutdown(rig):
    seen = []
    for code in [(0, 1), (1, 0), (1, 1), (2, 0), (2, 1), (2, 2), (2, 6), (2, 3),
                 (2, 4), (3, 0), (4, 0), (0, 1)]:
        rig.set_status(*code)
        rig.poll()
        seen.append(rig.value("phase"))
    assert seen == ["off"] + ["starting"] * 7 + ["warming_up", "running", "shutting_down", "off"]


@pytest.mark.parametrize("code", [(9, 9), (4, 7), (2, 5), (3, 1)])
def test_unknown_code_has_empty_phase_raw_code_and_one_warning(rig, warnings, code):
    rig.set_status(*code)
    rig.poll(3)
    raw = f"{code[0]}.{code[1]}"
    assert rig.value("phase") is None and rig.value("status_code") == raw
    assert sum(raw in w for w in warnings()) == 1


def test_unknown_code_refuses_start_and_accepts_stop(rig):
    rig.set_status(9, 9)
    rig.poll()
    rig.command("set", True)
    rig.poll()
    assert rig.frames(0x01) == []
    rig.command("set", False)
    rig.poll()
    assert len(rig.frames(0x03)) >= 1


@pytest.mark.parametrize(
    "major,minor,phase", [(3, 0, "running"), (2, 4, "warming_up"), (0, 1, "off")]
)
def test_error_code_does_not_change_phase(rig, major, minor, phase):
    rig.set_status(major, minor, error=30)
    rig.poll()
    assert rig.value("phase") == phase and rig.value("error_code") == 30


def test_scaling(rig):
    h = rig.heater
    h.status, h.internal, h.external = [3, 0], 0xFB, 22  # internal -5 C
    h.heater_byte, h.fan_byte, h.pump_byte, h.voltage_byte = 90, 70, 185, 132
    rig.poll()
    assert rig.value("internal_temperature_c") == -5
    assert rig.value("external_temperature_c") == 22
    assert rig.value("heater_temperature_c") == 75
    assert rig.value("fan_rpm") == 4200
    assert rig.value("fuel_pump_hz") == 1.85
    assert rig.value("voltage_v") == 13.2


def test_ten_byte_status_is_a_failed_poll(make_rig, warnings):
    rig = make_rig()
    rig.heater.status_payload_len10 = True
    rig.poll()
    assert rig.value("phase") is None
    assert any("10-byte" in w for w in warnings())


def test_live_fields_cleared_after_stale_after_s(rig):
    rig.heater.silent = True
    rig.poll(4)
    assert rig.value("phase") == "off"  # still within 5 s of the last good status
    rig.poll(2)
    for name in ("phase", "status_code", "voltage_v", "mode", "setpoint_c"):
        assert rig.value(name) is None
    assert rig.value("commands_enabled") is True  # identity survives


def test_recovers_and_resyncs_after_silence(rig):
    rig.heater.silent = True
    rig.poll(7)
    rig.heater.silent = False
    rig.heater.received.clear()
    rig.poll()
    assert [m for m, _ in rig.heater.received][:5] == [
        MSG_INIT,
        MSG_SERIAL,
        MSG_VERSION,
        MSG_SETTINGS,
        MSG_STATUS,
    ]
    assert rig.value("phase") == "off"


def test_published_values_validate_against_the_hub_schema(rig):
    jsonschema = pytest.importorskip("jsonschema")
    if not SCHEMA.exists():
        pytest.skip("hub repo not checked out beside driver-autoterm")
    rig.set_status(3, 35)
    rig.poll()
    entity = {
        n: rig.value(n)
        for n in (
            "driver",
            "name",
            "phase",
            "status_code",
            "error_code",
            "internal_temperature_c",
            "heater_temperature_c",
            "external_temperature_c",
            "voltage_v",
            "fan_rpm",
            "fuel_pump_hz",
            "ventilating",
            "mode",
            "setpoint_c",
            "power_level",
            "commands_enabled",
        )
        if rig.value(n) is not None
    }
    jsonschema.validate(entity, json.loads(SCHEMA.read_text()))

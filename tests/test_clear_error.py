"""clear_error/set: refused until `clear_error_frame` names a captured frame, then sent only from
off with a fault, confirmed by error 0, and never resent once the heater leaves off."""

import logging
from pathlib import Path

import pytest

from driver_autoterm.config import load_config, parse_clear_error_frame
from driver_autoterm.protocol import (
    MSG_SETTINGS,
    MSG_START,
    MSG_STATUS,
    MSG_STOP,
    MSG_UNBLOCK,
    MSG_VENTILATE,
)

UNKNOWN_MSG = 0x0E  # the fake heater ignores it, like a frame the real one does not act on


def no_burner_frames(rig) -> bool:
    return not rig.frames(MSG_START) and not rig.frames(MSG_VENTILATE)


@pytest.fixture
def faulted(make_rig):
    def build(frame=(MSG_UNBLOCK, b"")):
        rig = make_rig(clear_error_frame=frame)
        rig.set_status(0, 1, error=30)
        rig.poll()
        rig.heater.received.clear()
        return rig

    return build


# --- config ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", None),
        ("   ", None),
        ("0d", (0x0D, b"")),
        ("0D 00 01", (0x0D, b"\x00\x01")),
        ("0e01", (0x0E, b"\x01")),
    ],
)
def test_parse_clear_error_frame(text, expected):
    assert parse_clear_error_frame(text) == expected


@pytest.mark.parametrize("text", ["01", "02 01 00 04 10 00 08", "23 ff ff 04 ff"])
def test_frames_that_light_the_burner_or_write_settings_are_refused_at_load(text):
    with pytest.raises(ValueError, match="starts the heater or writes its settings"):
        parse_clear_error_frame(text)


@pytest.mark.parametrize("text", ["zz", "0d 0", "0d " + "00 " * 33])
def test_malformed_clear_error_frame_is_refused_at_load(text):
    with pytest.raises(ValueError):
        parse_clear_error_frame(text)


def test_clear_error_frame_defaults_to_unset_and_loads_from_config_ini(tmp_path: Path):
    assert load_config().device.clear_error_frame is None
    local = tmp_path / "config.ini"
    local.write_text("[device]\nclear_error_frame = 0d\n")
    assert load_config(local_path=local).device.clear_error_frame == (0x0D, b"")


# --- driver ------------------------------------------------------------------------------------


def test_unset_frame_keeps_clear_error_refused(make_rig, warnings):
    rig = make_rig()
    rig.set_status(0, 1, error=30)
    rig.poll()
    rig.command("clear_error/set", True)
    rig.poll(4)
    assert not rig.frames(MSG_UNBLOCK)
    assert rig.value("error_code") == 30
    assert [w for w in warnings() if "refused" in w and "clear_error_frame unset" in w]


def test_configured_frame_clears_the_error_once_and_never_starts(faulted, warnings):
    rig = faulted()
    rig.command("clear_error/set", True)
    rig.poll(3)
    assert rig.frames(MSG_UNBLOCK) == [b""]
    assert rig.value("error_code") == 0
    assert rig.value("phase") == "off"
    assert no_burner_frames(rig) and not rig.frames(MSG_STOP)
    assert not warnings()


def test_configured_payload_is_sent_as_captured(faulted):
    rig = faulted(frame=(MSG_UNBLOCK, b"\x00\x01"))
    rig.command("clear_error/set", True)
    rig.poll(2)
    assert rig.frames(MSG_UNBLOCK) == [b"\x00\x01"]


@pytest.mark.parametrize("payload", [False, 1, "true", None])
def test_only_true_is_accepted(faulted, payload, warnings):
    rig = faulted()
    rig.command("clear_error/set", payload)
    rig.poll(2)
    assert not rig.frames(MSG_UNBLOCK)
    assert [w for w in warnings() if "expected true" in w]


@pytest.mark.parametrize("status", [(2, 1), (3, 0), (3, 4), (4, 0)])
def test_refused_outside_off(faulted, status, warnings):
    rig = faulted()
    rig.set_status(*status, error=30)
    rig.poll()
    rig.command("clear_error/set", True)
    rig.poll(2)
    assert not rig.frames(MSG_UNBLOCK)
    assert [w for w in warnings() if "cannot clear an error in phase" in w]


def test_no_error_is_a_noop(make_rig, caplog):
    caplog.set_level(logging.INFO)
    rig = make_rig(clear_error_frame=(MSG_UNBLOCK, b""))
    rig.poll()
    rig.command("clear_error/set", True)
    rig.poll(2)
    assert not rig.frames(MSG_UNBLOCK)
    assert "Clear error ignored: heater reports no error" in caplog.text


def test_frame_the_heater_ignores_is_retried_once_then_one_warning(faulted, warnings):
    rig = faulted(frame=(UNKNOWN_MSG, b""))
    rig.command("clear_error/set", True)
    rig.poll(10)
    assert len(rig.frames(UNKNOWN_MSG)) == 2
    assert rig.value("error_code") == 30
    assert sum("clear_error unconfirmed" in w for w in warnings()) == 1
    assert no_burner_frames(rig)


def test_heater_leaving_off_ends_the_wait_with_one_error_and_no_resend(faulted, caplog):
    caplog.set_level(logging.WARNING)
    rig = faulted(frame=(UNKNOWN_MSG, b""))
    rig.command("clear_error/set", True)
    rig.poll()
    rig.set_status(2, 1, error=0)  # as if the frame had started the heater
    rig.poll(10)
    assert len(rig.frames(UNKNOWN_MSG)) == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [
        "clear_error answered with status 2.1 (phase starting), not off: not retrying"
    ]
    # The Driver still never stops the heater by itself (ADR 0021).
    assert not rig.frames(MSG_STOP)


def test_only_polls_follow_a_confirmed_clear(faulted):
    rig = faulted()
    rig.command("clear_error/set", True)
    rig.poll(12)
    other = {m for m, _ in rig.heater.received} - {MSG_STATUS, MSG_SETTINGS, MSG_UNBLOCK}
    assert not other
    assert len(rig.frames(MSG_UNBLOCK)) == 1

import pytest

from driver_autoterm.protocol import (
    MSG_SETTINGS,
    MSG_START,
    MSG_STATUS,
    MSG_STOP,
    MSG_UNBLOCK,
    MSG_VENTILATE,
)


def sent(rig, msg):
    return rig.frames(msg)


# --- gating -----------------------------------------------------------------------
def test_commands_disabled_publishes_telemetry_but_rejects_everything(make_rig, warnings):
    rig = make_rig(commands_enabled=False)
    rig.poll()
    assert rig.value("commands_enabled") is False and rig.value("phase") == "off"
    for suffix, payload in [
        ("set", True),
        ("set", False),
        ("mode/set", "power"),
        ("setpoint_c/set", 20),
        ("power_level/set", 3),
        ("ventilating/set", True),
        ("clear_error/set", True),
    ]:
        rig.command(suffix, payload)
    rig.poll()
    assert not sent(rig, MSG_START) and not sent(rig, MSG_STOP) and not sent(rig, MSG_VENTILATE)
    assert len(sent(rig, MSG_SETTINGS)) == 1  # only the connect read
    assert sum("commands_enabled is false" in w for w in warnings()) == 7


def test_commands_rejected_until_synced(make_rig, warnings):
    rig = make_rig()
    rig.command("set", True)  # before the first poll: nothing read yet
    rig.poll()
    assert not sent(rig, MSG_START)
    assert any("not yet synced" in w for w in warnings())


def test_commands_rejected_when_stale(rig, warnings):
    rig.heater.silent = True
    rig.poll(6)
    rig.command("set", False)
    rig.poll()
    assert not sent(rig, MSG_STOP)
    assert any("stale" in w for w in warnings())


@pytest.mark.parametrize("state", ["disabled", "stale"])
def test_stop_is_rejected_with_a_warning_when_disabled_or_stale(make_rig, state, warnings):
    rig = make_rig(commands_enabled=state != "disabled")
    rig.poll()
    if state == "stale":
        rig.heater.silent = True
        rig.poll(6)
    rig.command("set", False)
    rig.poll()
    assert not sent(rig, MSG_STOP)
    assert any("commands_enabled is false" in w or "stale" in w for w in warnings())


# --- start ------------------------------------------------------------------------
def test_start_from_off_uses_last_read_settings_and_is_confirmed(rig):
    rig.command("set", True)
    rig.poll()
    assert sent(rig, MSG_START) == [bytes([1, 0, 4, 16, 0, 8])]
    rig.poll()
    assert rig.value("phase") == "starting"
    rig.poll(6)
    assert len(sent(rig, MSG_START)) == 1  # confirmed: no retry


@pytest.mark.parametrize("status", [(2, 1), (2, 4), (3, 0)])
def test_start_while_already_on_is_a_noop(rig, status):
    rig.set_status(*status)
    rig.poll()
    rig.command("set", True)
    rig.poll()
    assert not sent(rig, MSG_START)


@pytest.mark.parametrize("status", [(3, 4), (4, 0), (3, 35)])
def test_start_rejected_while_shutting_down_or_ventilating(rig, status, warnings):
    rig.set_status(*status)
    rig.poll()
    rig.command("set", True)
    rig.poll()
    assert not sent(rig, MSG_START) and warnings()


def test_start_rejected_with_error(rig, warnings):
    rig.set_status(0, 1, error=5)
    rig.poll()
    rig.command("set", True)
    rig.poll()
    assert not sent(rig, MSG_START)
    assert any("error 5" in w for w in warnings())


# --- stop -------------------------------------------------------------------------
def test_stop_is_accepted_even_with_an_error_and_sent_once_it_takes(rig):
    rig.set_status(3, 0, error=5)
    rig.poll()
    rig.command("set", False)
    rig.poll(4)
    assert len(sent(rig, MSG_STOP)) == 1  # first stop took: heater is shutting down
    assert rig.value("phase") == "shutting_down"


def _stuck_stop(rig):
    rig.set_status(3, 0)
    rig.poll()
    rig.heater.stop_effective = False
    rig.command("set", False)


def test_stop_that_never_takes_is_tried_3_times_3_polls_apart_then_one_error(rig, caplog):
    _stuck_stop(rig)
    rig.poll(1)  # command accepted: attempt 1
    assert len(sent(rig, MSG_STOP)) == 1
    rig.poll(2)
    assert len(sent(rig, MSG_STOP)) == 1
    rig.poll(1)  # 3 polls after attempt 1
    assert len(sent(rig, MSG_STOP)) == 2
    rig.poll(3)
    assert len(sent(rig, MSG_STOP)) == 3
    rig.poll(20)
    assert len(sent(rig, MSG_STOP)) == 3
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1


def test_new_stop_command_restarts_the_attempts(rig, caplog):
    _stuck_stop(rig)
    rig.poll(15)
    assert len(sent(rig, MSG_STOP)) == 3
    rig.command("set", False)
    rig.poll(15)
    assert len(sent(rig, MSG_STOP)) == 6
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 2


def test_stop_that_takes_on_a_retry_stops_retrying(rig):
    _stuck_stop(rig)
    rig.poll(2)
    rig.heater.stop_effective = True
    rig.poll(10)
    assert rig.value("phase") == "shutting_down"
    assert len(sent(rig, MSG_STOP)) == 2


@pytest.mark.parametrize("status", [(1, 0), (2, 1), (2, 4), (4, 0), (0, 1)])
def test_stop_is_accepted_in_every_phase(rig, status):
    rig.set_status(*status)
    rig.poll()
    rig.command("set", False)
    rig.poll()
    assert len(sent(rig, MSG_STOP)) == 1


# --- refused until bench-verified ------------------------------------------------------
@pytest.mark.parametrize("suffix", ["ventilating/set", "clear_error/set"])
@pytest.mark.parametrize("payload", [True, False])
@pytest.mark.parametrize("status", [(0, 1, 0), (0, 1, 5), (3, 35, 0)])
def test_unverified_commands_are_refused_with_one_warning(rig, suffix, payload, status, warnings):
    rig.set_status(status[0], status[1], error=status[2])
    rig.poll()
    rig.heater.received.clear()
    rig.command(suffix, payload)
    rig.poll(4)
    assert {m for m, _ in rig.heater.received} == {MSG_STATUS}  # no frame, 0x0D never sent
    assert not sent(rig, MSG_UNBLOCK) and not sent(rig, MSG_VENTILATE)
    assert len([w for w in warnings() if "refused" in w]) == 1


# --- settings -----------------------------------------------------------------------
def test_setpoint_writes_full_settings_and_reads_back(rig):
    rig.command("setpoint_c/set", 22)
    rig.poll()
    assert sent(rig, MSG_SETTINGS)[1] == bytes([1, 0, 4, 22, 0, 8])
    assert len(sent(rig, MSG_SETTINGS)) == 2  # no immediate read: the next poll reads it back
    rig.poll()
    assert len(sent(rig, MSG_SETTINGS)) == 3
    assert rig.value("setpoint_c") == 22


def test_mode_and_power_level_change_one_field_each(rig):
    rig.command("mode/set", "external_temperature")
    rig.poll(2)
    rig.command("power_level/set", 3)
    rig.poll(2)
    writes = [p for p in sent(rig, MSG_SETTINGS) if p]
    assert writes[0] == bytes([1, 0, 3, 16, 0, 8]) and writes[1] == bytes([1, 0, 3, 16, 0, 3])
    assert (rig.value("mode"), rig.value("power_level")) == ("external_temperature", 3)


@pytest.mark.parametrize("status", [(3, 4), (3, 35)])
def test_settings_refused_while_shutting_down_or_ventilating(rig, status, warnings):
    rig.set_status(*status)
    rig.poll()
    rig.command("setpoint_c/set", 20)
    rig.poll()
    assert [p for p in sent(rig, MSG_SETTINGS) if p] == []  # no write, only the connect read
    assert warnings()


def test_settings_allowed_while_running(rig):
    rig.set_status(3, 0)
    rig.poll()
    rig.command("power_level/set", 5)
    rig.poll(2)
    assert rig.value("power_level") == 5


# --- malformed payloads ---------------------------------------------------------------
@pytest.mark.parametrize(
    "suffix,payload",
    [
        ("set", "not json"),
        ("set", "1"),
        ("set", '"true"'),
        ("set", "null"),
        ("mode/set", '"controller_temperature"'),
        ("mode/set", '"bogus"'),
        ("mode/set", "4"),
        ("setpoint_c/set", "0"),
        ("setpoint_c/set", "31"),
        ("setpoint_c/set", "20.5"),
        ("setpoint_c/set", "20.0"),
        ("setpoint_c/set", "true"),
        ("setpoint_c/set", '"20"'),
        ("power_level/set", "10"),
        ("power_level/set", "-1"),
        ("power_level/set", "2.5"),
    ],
)
def test_malformed_commands_send_no_frame(rig, suffix, payload, warnings):
    rig.heater.received.clear()
    rig.command(suffix, payload, raw=True)
    rig.poll()
    assert {m for m, _ in rig.heater.received} == {MSG_STATUS}  # the poll itself, nothing else
    assert warnings()


# --- confirmation and retry -----------------------------------------------------------
def test_unconfirmed_start_retries_once_after_three_polls_then_warns(rig, warnings):
    rig.heater.ignore[MSG_START] = 2  # swallow the first send and the retry
    rig.command("set", True)
    rig.poll()
    assert len(sent(rig, MSG_START)) == 1
    rig.poll(2)
    assert len(sent(rig, MSG_START)) == 1
    rig.poll()
    assert len(sent(rig, MSG_START)) == 2  # one retry, no blind double-send before it
    rig.poll(10)
    assert len(sent(rig, MSG_START)) == 2
    unconfirmed = [w for w in warnings() if "unconfirmed" in w]
    assert len(unconfirmed) == 1 and "start" in unconfirmed[0] and "off" in unconfirmed[0]


def test_retry_that_lands_is_confirmed_without_warning(rig, warnings):
    rig.heater.ignore[MSG_START] = 1
    rig.command("set", True)
    rig.poll(5)
    assert len(sent(rig, MSG_START)) == 2 and rig.value("phase") == "starting"
    assert not any("unconfirmed" in w for w in warnings())


def test_settings_write_matching_read_back_has_no_warning(rig, warnings):
    rig.set_status(3, 0)
    rig.poll()
    rig.command("setpoint_c/set", 20)
    rig.poll(6)
    assert rig.value("setpoint_c") == 20
    assert not warnings()


def test_settings_write_mismatch_warns_once_without_retry_or_new_topic(rig, warnings):
    rig.set_status(3, 0)
    rig.poll()
    topics = set(rig.pub.retained)
    rig.heater.ignore[MSG_SETTINGS] = 1  # the write is lost; the read-back shows the old value
    rig.command("setpoint_c/set", 22)
    rig.poll(10)
    assert len([p for p in sent(rig, MSG_SETTINGS) if p]) == 1  # no second attempt
    assert len(sent(rig, MSG_SETTINGS)) == 3  # connect read, write, one confirming read
    assert rig.value("setpoint_c") == 16  # the read-back is what is published
    assert set(rig.pub.retained) == topics
    unconfirmed = [w for w in warnings() if "unconfirmed" in w]
    assert len(unconfirmed) == 1
    assert "setpoint_c" in unconfirmed[0] and "running" in unconfirmed[0]


# --- restart ----------------------------------------------------------------------------
def test_no_recommand_after_restart(make_rig):
    first = make_rig()
    first.poll()
    first.command("set", True)
    first.poll(2)
    # New Driver process against the same (now running) heater.
    second = make_rig()
    second.heater = first.heater
    second.driver._open_stream = lambda: first.heater
    first.heater.received.clear()
    second.poll(5)
    assert second.value("phase") == "starting"
    assert not sent(second, MSG_START) and not sent(second, MSG_STOP)
    assert {m for m, _ in first.heater.received} <= {MSG_STATUS, MSG_SETTINGS, 0x1C, 0x04, 0x06}


# --- init frames --------------------------------------------------------------------
def test_default_config_sends_no_init_frames_and_enabling_it_does(make_rig):
    from driver_autoterm.config import load_config

    assert load_config().device.send_init is False
    off = make_rig(send_init=False)
    off.poll()
    on = make_rig(send_init=True)
    on.poll()
    assert {m for m, _ in off.heater.received} == {MSG_STATUS, MSG_SETTINGS}
    assert len(on.heater.received) > len(off.heater.received)

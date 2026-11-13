"""Poll the heater over UART, publish the `heater` Entity, and (only when enabled) obey the six
command topics under the safety rules of hub ADR 0021.

Serial access is single-threaded: MQTT callbacks only enqueue commands, `poll()` drains them.
The Driver never starts, stops or re-commands the heater on its own.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import time
from dataclasses import dataclass, replace
from typing import Callable

from driver_autoterm.config import AppConfig, DeviceConfig
from driver_autoterm.driver_info import build_info, read_board_serial
from driver_autoterm.link import HeaterLink, Stream, open_serial
from driver_autoterm.protocol import (
    CODE_BY_MODE,
    WAIT_MODE_THERMOSTAT_OFF,
    WAIT_MODE_THERMOSTAT_ON,
    MSG_INIT,
    MSG_SERIAL,
    MSG_SETTINGS,
    MSG_START,
    MSG_STATUS,
    MSG_STOP,
    MSG_VENTILATE,
    MSG_VERSION,
    Settings,
    Status,
    encode_settings,
    parse_settings,
    parse_status,
)
from driver_autoterm.publisher import Publisher

logger = logging.getLogger(__name__)

DRIVER_KEY = "autoterm"
COMMAND_TOPICS = (
    "set",
    "mode/set",
    "setpoint_c/set",
    "power_level/set",
    "thermostat/set",
    "ventilating/set",
    "clear_error/set",
    "commands_enabled/set",
)
LIVE_FIELDS = (
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
    "thermostat",
)
# Polls without confirmation before the single retry, and again before giving up.
CONFIRM_POLLS = 3
# Stop is sent at most this many times, STOP_RETRY_POLLS polls apart, then one ERROR.
STOP_ATTEMPTS = 3
STOP_RETRY_POLLS = 3
# The settings (0x02) are re-read every this many polls, so a change made on the panel is
# published and logged; every command that writes them also reads them fresh first.
SETTINGS_EVERY_POLLS = 5

# (major, minor) -> phase. Major 0 is "off" for any minor. 3.35 is an unverified default; every
# other listed code was seen on a real AIR 2D: the start-run-stop cycle on 2026-11-02, and the
# Thermostat standby 3.4 (burner off, purging) then 3.5 (waiting to re-ignite) on 2026-11-13.
PHASES: dict[tuple[int, int], str] = {
    (1, 0): "starting",
    (1, 1): "starting",
    (2, 0): "starting",
    (2, 1): "starting",
    (2, 2): "starting",
    (2, 6): "starting",
    (2, 3): "starting",
    (2, 4): "warming_up",
    (3, 0): "running",
    (3, 35): "running",  # unverified: fan-only, also published as ventilating
    (3, 4): "standby",
    (3, 5): "standby",
    (4, 0): "shutting_down",
}


# The 0x0D unblock is never sent on its own. `clear_error/set` sends only the frame named by
# `clear_error_frame` (captured from the panel, docs/bench-clear-error.md), refused without it.


def _topic(device_id: str, prop: str) -> str:
    return f"renewvan/heater/{device_id}/{prop}"


def map_phase(major: int, minor: int) -> str | None:
    """Phase for a status code, or None when the code is not in the table."""
    if major == 0:
        return "off"
    return PHASES.get((major, minor))


class Rejected(Exception):
    """A command refused before any frame was sent; the message is the logged reason."""


@dataclass
class _Pending:
    send: Callable[[], None]
    confirmed: Callable[[Status, Settings | None], bool]
    name: str = "command"
    polls: int = 0
    retried: bool = False


class HeaterDriver:
    def __init__(
        self,
        config: DeviceConfig,
        publisher,  # noqa: ANN001 - Publisher or a test double
        open_stream: Callable[[], Stream],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = config
        self._pub = publisher
        self._open_stream = open_stream
        self._clock = clock
        # Runtime switch, seeded from the config file. `commands_enabled/set` flips it for this run
        # only, so a restart falls back to the file (fail closed).
        self._commands_enabled = config.commands_enabled
        self._link: HeaterLink | None = None
        self._commands: queue.Queue[tuple[str, str]] = queue.Queue()
        self._unknown_codes: set[str] = set()
        self._last_settings_raw: bytes | None = None
        self._started_at = clock()
        self._reset_link_state()
        self._last_valid: float | None = None
        self._stale = False

    # --- lifecycle ----------------------------------------------------------------
    def start(self) -> None:
        self._pub.publish(_topic(self._cfg.id, "driver"), json.dumps(DRIVER_KEY))
        if self._cfg.name:
            self._pub.publish(_topic(self._cfg.id, "name"), json.dumps(self._cfg.name))
        self._pub.publish(
            _topic(self._cfg.id, "commands_enabled"), json.dumps(self._commands_enabled)
        )
        # Retained values from a previous run must not outlive this one's knowledge.
        self._clear_live()
        for suffix in COMMAND_TOPICS:
            self._pub.subscribe(
                _topic(self._cfg.id, suffix),
                lambda payload, s=suffix: self._commands.put((s, payload)),
            )

    def _reset_link_state(self) -> None:
        self._synced = False
        self._settings: Settings | None = None
        self._status: Status | None = None
        self._pending: _Pending | None = None
        self._stop_pending = False
        self._stop_attempts = 0
        self._stop_polls = 0
        # The fields a settings write changed, awaiting the next 0x02 read.
        self._written: tuple[dict[str, int], str] | None = None
        self._polls_since_settings = 0

    # --- one cycle ----------------------------------------------------------------
    def poll(self) -> None:
        now = self._clock()
        if not self._synced:
            # Queued while there was no fresh state: reject now rather than act on it later.
            self._drain_commands()
        status = self._refresh()
        if status is not None:
            self._last_valid = now
            self._stale = False
            self._publish_live()
        elif not self._stale and now - (self._last_valid or self._started_at) > (
            self._cfg.stale_after_s
        ):
            self._go_stale()

        # Earlier commands first: a command accepted this poll starts its clock next poll.
        if self._status is not None and not self._stale:
            self._service_stop()
            self._service_pending()
        self._drain_commands()

    def _refresh(self) -> Status | None:
        try:
            if not self._synced:
                return self._connect()
            status = self._read_status()
            if status is not None and self._written is not None:
                self._confirm_settings()
            elif status is not None:
                self._polls_since_settings += 1
                if self._polls_since_settings >= SETTINGS_EVERY_POLLS:
                    self._read_settings()
            return status
        except OSError as exc:
            logger.warning("Serial error on %s: %s", self._cfg.serial_device, exc)
            self._drop_link()
            return None

    def _confirm_settings(self) -> None:
        """Compare the written field with this poll's 0x02 read: one WARNING on mismatch, no retry.

        Only the commanded field counts: the heater rewrites power_level by itself while running in
        a temperature mode, which is not a failed write."""
        assert self._written is not None and self._status is not None
        change, name = self._written
        self._written = None
        if not self._read_settings():
            self._warn_unconfirmed(name)
            return
        assert self._settings is not None
        if any(getattr(self._settings, k) != v for k, v in change.items()):
            self._warn_unconfirmed(name)

    def _ensure_link(self) -> HeaterLink:
        if self._link is None:
            self._link = HeaterLink(self._open_stream(), self._cfg.reply_timeout_s)
        return self._link

    def _drop_link(self) -> None:
        if self._link is not None:
            try:
                self._link.close()
            except OSError:
                pass
        self._link = None
        self._synced = False

    def _connect(self) -> Status | None:
        """Init (optional), settings, then telemetry; commands stay rejected until all are read."""
        link = self._ensure_link()
        if self._cfg.send_init:
            for msg in (MSG_INIT, MSG_SERIAL, MSG_VERSION):
                link.transact(msg)  # replies are read and discarded; a missing one is tolerated
        settings = self._parse_settings_reply(link.transact(MSG_SETTINGS))
        if settings is None:
            logger.warning("Heater did not report its settings; will retry")
            return None
        self._settings = settings
        status = self._read_status()
        if status is not None:
            self._synced = True
        return status

    def _read_status(self) -> Status | None:
        reply = self._ensure_link().transact(MSG_STATUS)
        if reply is None:
            return None
        status = parse_status(reply.payload)
        if status is None:
            logger.warning(
                "Discarding status reply with %d-byte payload (expected 19; wrong heater model?)",
                len(reply.payload),
            )
            return None
        self._status = status
        return status

    def _go_stale(self) -> None:
        logger.warning("No valid status for %s s: clearing live fields", self._cfg.stale_after_s)
        self._stale = True
        self._reset_link_state()
        self._clear_live()

    # --- publishing -----------------------------------------------------------------
    def _phase(self, status: Status) -> str | None:
        phase = map_phase(status.major, status.minor)
        if phase is None:
            if status.status_code not in self._unknown_codes:
                self._unknown_codes.add(status.status_code)
                logger.warning(
                    "Unknown status code %s: publishing an empty phase", status.status_code
                )
        return phase

    def _publish_live(self) -> None:
        s = self._status
        assert s is not None
        values: dict[str, object | None] = {
            "phase": self._phase(s),  # None publishes an empty retained value
            "status_code": s.status_code,
            "error_code": s.error_code,
            "internal_temperature_c": s.internal_temperature_c,
            "heater_temperature_c": s.heater_temperature_c,
            "external_temperature_c": s.external_temperature_c,
            "voltage_v": s.voltage_v,
            "fan_rpm": s.fan_rpm,
            "fuel_pump_hz": s.fuel_pump_hz,
            "ventilating": True if (s.major, s.minor) == (3, 35) else None,
            "mode": None,
            "setpoint_c": None,
            "power_level": None,
            "thermostat": None,
        }
        if self._settings is not None:
            st = self._settings
            values["mode"] = st.mode
            values["setpoint_c"] = st.setpoint_c if 1 <= st.setpoint_c <= 30 else None
            values["power_level"] = st.power_level if 0 <= st.power_level <= 9 else None
            values["thermostat"] = st.thermostat
        for name, value in values.items():
            self._pub.publish(
                _topic(self._cfg.id, name), "" if value is None else json.dumps(value)
            )

    def _clear_live(self) -> None:
        for name in LIVE_FIELDS:
            self._pub.publish(_topic(self._cfg.id, name), "")

    # --- commands -------------------------------------------------------------------
    def _drain_commands(self) -> None:
        while True:
            try:
                suffix, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._execute(suffix, payload)
            except Rejected as exc:
                logger.warning("Rejected command on %s: %s", _topic(self._cfg.id, suffix), exc)
            except OSError as exc:
                logger.warning("Serial error handling %s: %s", suffix, exc)
                self._drop_link()

    def _set_commands_enabled(self, payload: str) -> None:
        """The one command accepted while commands are off, since it is what turns them on."""
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            raise Rejected(f"invalid JSON {payload!r}") from None
        self._expect_bool(value)
        if value != self._commands_enabled:
            logger.warning("commands_enabled set to %s over MQTT (until restart)", value)
        self._commands_enabled = value
        self._pub.publish(_topic(self._cfg.id, "commands_enabled"), json.dumps(value))

    def _execute(self, suffix: str, payload: str) -> None:
        if suffix == "commands_enabled/set":
            self._set_commands_enabled(payload)
            return
        if not self._commands_enabled:
            raise Rejected("commands_enabled is false")
        if self._stale or not self._synced or self._status is None or self._settings is None:
            raise Rejected("heater state is stale or not yet synced")
        if suffix == "clear_error/set" and self._cfg.clear_error_frame is None:
            raise Rejected(
                f"{suffix} is refused until its frame is bench-verified (clear_error_frame unset)"
            )
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            raise Rejected(f"invalid JSON {payload!r}") from None

        status = self._status
        phase = self._phase(status)
        ventilating = (status.major, status.minor) == (3, 35)
        settings = self._settings

        stopping = suffix in ("set", "ventilating/set") and value is False
        if phase is None and not stopping:
            raise Rejected(f"unknown status code {status.status_code}: only stop is accepted")

        if suffix == "set":
            self._expect_bool(value)
            if value:
                self._start(status, phase, ventilating, settings)
            else:
                self._stop()
        elif suffix == "ventilating/set":
            self._expect_bool(value)
            if value:
                self._ventilate(phase, settings)
            else:
                self._stop()
        elif suffix == "clear_error/set":
            self._clear_error(value, status, phase)
        else:
            self._settings_command(suffix, value, phase, ventilating, settings)

    @staticmethod
    def _expect_bool(value: object) -> None:
        if not isinstance(value, bool):
            raise Rejected(f"expected true or false, got {value!r}")

    def _start(
        self, status: Status, phase: str | None, ventilating: bool, settings: Settings
    ) -> None:
        if ventilating or phase == "shutting_down":
            raise Rejected(f"cannot start while {'ventilating' if ventilating else phase}")
        if phase != "off":
            logger.info("Start ignored: heater already %s", phase)
            return
        if status.error_code != 0:
            raise Rejected(f"heater reports error {status.error_code}")
        settings = self._fresh_settings()
        self._send_pending(
            lambda: self._send(MSG_START, encode_settings(settings)),
            lambda s, _st: self._phase(s) != "off",
            name="start",
        )

    def _ventilate(self, phase: str | None, settings: Settings) -> None:
        """Fan-only (status 3.35), from off only, at the power level the heater reported."""
        if phase != "off":
            raise Rejected(f"cannot ventilate from phase {phase}")
        level = self._fresh_settings().power_level
        self._send_pending(
            lambda: self._send(MSG_VENTILATE, bytes([0xFF, 0xFF, level, 0xFF])),
            self._ventilation_confirmed,
            name="ventilate",
        )

    def _ventilation_confirmed(self, status: Status, _settings: Settings | None) -> bool:
        """Only fan-only (3.35) confirms. Any other start ends the wait with one ERROR and no retry:
        the 0x23 frame is unverified, and a frame that lit the burner must not be sent again."""
        if (status.major, status.minor) == (3, 35):
            return True
        if self._phase(status) == "off":
            return False
        logger.error(
            "Ventilate answered with status %s (phase %s), not fan-only 3.35: not retrying",
            status.status_code,
            self._phase(status),
        )
        return True

    def _clear_error(self, value: object, status: Status, phase: str | None) -> None:
        """Send the configured clear frame, from off with a fault only; confirmed by error 0."""
        if value is not True:
            raise Rejected(f"expected true, got {value!r}")
        if phase != "off":
            raise Rejected(f"cannot clear an error in phase {phase}")
        if status.error_code == 0:
            logger.info("Clear error ignored: heater reports no error")
            return
        assert self._cfg.clear_error_frame is not None
        msg, payload = self._cfg.clear_error_frame
        logger.info(
            "Clearing error %d with frame %s", status.error_code, bytes([msg, *payload]).hex(" ")
        )
        self._send_pending(
            lambda: self._send(msg, payload), self._clear_confirmed, name="clear_error"
        )

    def _clear_confirmed(self, status: Status, _settings: Settings | None) -> bool:
        """Error 0 while off confirms. Leaving off ends the wait with one ERROR and no retry: a
        frame that moved the heater out of off must not be sent again (as for ventilate)."""
        phase = self._phase(status)
        if phase != "off":
            logger.error(
                "clear_error answered with status %s (phase %s), not off: not retrying",
                status.status_code,
                phase,
            )
            return True
        return status.error_code == 0

    def _stop(self) -> None:
        self._stop_pending = True
        self._stop_attempts = 1
        self._stop_polls = 0
        self._pending = None
        self._send(MSG_STOP)

    def _settings_command(
        self, suffix: str, value: object, phase: str | None, ventilating: bool, settings: Settings
    ) -> None:
        if suffix == "mode/set":
            if not isinstance(value, str) or value not in CODE_BY_MODE:
                raise Rejected(f"unknown mode {value!r}")
            change = {"mode_code": CODE_BY_MODE[value]}
        elif suffix == "setpoint_c/set":
            self._expect_int(value, 1, 30)
            change = {"setpoint_c": value}
        elif suffix == "power_level/set":
            self._expect_int(value, 0, 9)
            change = {"power_level": value}
        elif suffix == "thermostat/set":
            self._expect_bool(value)
            change = {"wait_mode": WAIT_MODE_THERMOSTAT_ON if value else WAIT_MODE_THERMOSTAT_OFF}
        else:
            raise Rejected("unknown command topic")
        if ventilating or phase == "shutting_down":
            blocker = "ventilating" if ventilating else phase
            raise Rejected(f"settings cannot change while {blocker}")
        new = replace(self._fresh_settings(), **change)
        self._stop_pending = False
        self._send(MSG_SETTINGS, encode_settings(new))
        # Confirmed by the next regular poll's 0x02 read, so the heater has time to apply it.
        self._written = (change, suffix.removesuffix("/set"))

    @staticmethod
    def _expect_int(value: object, low: int, high: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise Rejected(f"expected an integer, got {value!r}")
        if not low <= value <= high:
            raise Rejected(f"{value} outside {low}-{high}")

    # --- sending, retry and confirmation ----------------------------------------------
    def _send(self, msg: int, payload: bytes = b"") -> None:
        self._ensure_link().transact(msg, payload)

    def _fresh_settings(self) -> Settings:
        """The heater's settings read just now. Start, ventilate and settings writes carry the whole
        payload, so they are never based on a copy older than a change made on the panel."""
        if not self._read_settings():
            raise Rejected("heater did not report its settings just now")
        assert self._settings is not None
        return self._settings

    def _read_settings(self) -> bool:
        """Read 0x02 into `_settings`; False (keeping the old copy) when it gave no valid reply."""
        self._polls_since_settings = 0
        reply = self._ensure_link().transact(MSG_SETTINGS)
        settings = self._parse_settings_reply(reply)
        if settings is None:
            return False
        self._settings = settings
        return True

    def _parse_settings_reply(self, reply) -> Settings | None:  # noqa: ANN001 - link Frame or None
        """Parse a 0x02 reply and log its raw bytes: DEBUG always, INFO when they change.

        The raw bytes are how modes the driver cannot command yet (thermostat, heat + ventilation,
        picked on the panel) are told apart from the ones it knows.
        """
        if reply is None:
            return None
        raw = bytes(reply.payload)
        logger.debug("Settings (0x02) payload: %s", raw.hex(" "))
        if self._last_settings_raw is not None and raw != self._last_settings_raw:
            logger.info(
                "Heater settings changed: %s -> %s", self._last_settings_raw.hex(" "), raw.hex(" ")
            )
        self._last_settings_raw = raw
        return parse_settings(reply.payload)

    def _send_pending(
        self,
        send: Callable[[], None],
        confirmed: Callable[[Status, Settings | None], bool],
        name: str = "command",
    ) -> None:
        self._stop_pending = False
        send()
        self._read_settings()
        assert self._status is not None
        self._pending = _Pending(send, confirmed, name)
        self._publish_live()

    def _service_stop(self) -> None:
        if not self._stop_pending:
            return
        assert self._status is not None
        if self._phase(self._status) in ("shutting_down", "off"):
            self._stop_pending = False
            return
        self._stop_polls += 1
        if self._stop_polls < STOP_RETRY_POLLS:
            return
        if self._stop_attempts >= STOP_ATTEMPTS:
            logger.error("Heater did not take stop after %d attempts; giving up", STOP_ATTEMPTS)
            self._stop_pending = False
            return
        self._stop_attempts += 1
        self._stop_polls = 0
        self._send(MSG_STOP)

    def _service_pending(self) -> None:
        pending = self._pending
        if pending is None or self._status is None:
            return
        if pending.confirmed(self._status, self._settings):
            self._pending = None
            self._publish_live()
            return
        pending.polls += 1
        if pending.polls < CONFIRM_POLLS:
            return
        if not pending.retried:
            logger.info("Command not confirmed after %d polls: retrying once", CONFIRM_POLLS)
            pending.retried = True
            pending.polls = 0
            pending.send()
        else:
            self._warn_unconfirmed(pending.name)
            self._pending = None

    def _warn_unconfirmed(self, name: str) -> None:
        assert self._status is not None
        logger.warning(
            "Command %s unconfirmed in phase %s; giving up", name, self._phase(self._status)
        )


def run(config: AppConfig) -> None:
    info = build_info(os.environ.get("DRIVER_VERSION"), read_board_serial())
    publisher = Publisher(config.mqtt, info)
    driver = HeaterDriver(
        config.device, publisher, lambda: open_serial(config.device.serial_device)
    )
    publisher.connect()
    driver.start()
    try:
        while True:
            driver.poll()
            time.sleep(config.device.update_interval_ms / 1000)
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        publisher.disconnect()

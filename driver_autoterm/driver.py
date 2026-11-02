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
from dataclasses import dataclass
from typing import Callable

from driver_autoterm.config import AppConfig, DeviceConfig
from driver_autoterm.driver_info import build_info, read_board_serial
from driver_autoterm.link import HeaterLink, Stream, open_serial
from driver_autoterm.protocol import (
    CODE_BY_MODE,
    MSG_INIT,
    MSG_SERIAL,
    MSG_SETTINGS,
    MSG_START,
    MSG_STATUS,
    MSG_STOP,
    MSG_UNBLOCK,
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
    "ventilating/set",
    "clear_error/set",
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
)
# Polls without confirmation before the single retry, and again before giving up.
CONFIRM_POLLS = 3
# Stop is sent at most this many times, STOP_RETRY_POLLS polls apart, then one ERROR.
STOP_ATTEMPTS = 3
STOP_RETRY_POLLS = 3


def _topic(device_id: str, prop: str) -> str:
    return f"renewvan/heater/{device_id}/{prop}"


def map_phase(major: int, minor: int) -> str | None:
    """Phase for a status code, or None outside the mapping (only 0.1, 3.35 verified)."""
    if major == 0:
        return "off"
    if major == 1:
        return "starting"
    if major == 2:
        if 1 <= minor <= 3:
            return "starting"
        return "warming_up" if minor == 4 else None
    if major == 3:
        if minor in (0, 35):
            return "running"
        return "shutting_down" if minor == 4 else None
    if major == 4:
        return "shutting_down"
    return None


class Rejected(Exception):
    """A command refused before any frame was sent; the message is the logged reason."""


@dataclass
class _Pending:
    send: Callable[[], None]
    confirmed: Callable[[Status, Settings | None], bool]
    reread_settings: bool = False
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
        self._link: HeaterLink | None = None
        self._commands: queue.Queue[tuple[str, str]] = queue.Queue()
        self._unknown_codes: set[str] = set()
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
            _topic(self._cfg.id, "commands_enabled"), json.dumps(self._cfg.commands_enabled)
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
            return self._read_status()
        except OSError as exc:
            logger.warning("Serial error on %s: %s", self._cfg.serial_device, exc)
            self._drop_link()
            return None

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
        reply = link.transact(MSG_SETTINGS)
        settings = parse_settings(reply.payload) if reply else None
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
    def _phase(self, status: Status) -> str:
        phase = map_phase(status.major, status.minor)
        if phase is None:
            if status.status_code not in self._unknown_codes:
                self._unknown_codes.add(status.status_code)
                logger.warning("Unknown status code %s: publishing phase off", status.status_code)
            return "off"
        return phase

    def _publish_live(self) -> None:
        s = self._status
        assert s is not None
        values: dict[str, object | None] = {
            "phase": self._phase(s),
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
        }
        if self._settings is not None:
            st = self._settings
            values["mode"] = st.mode
            values["setpoint_c"] = st.setpoint_c if 1 <= st.setpoint_c <= 30 else None
            values["power_level"] = st.power_level if 0 <= st.power_level <= 9 else None
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

    def _execute(self, suffix: str, payload: str) -> None:
        if not self._cfg.commands_enabled:
            raise Rejected("commands_enabled is false")
        if self._stale or not self._synced or self._status is None or self._settings is None:
            raise Rejected("heater state is stale or not yet synced")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            raise Rejected(f"invalid JSON {payload!r}") from None

        status = self._status
        phase = self._phase(status)
        ventilating = (status.major, status.minor) == (3, 35)
        settings = self._settings

        if suffix == "set":
            self._expect_bool(value)
            if value:
                self._start(status, phase, ventilating, settings)
            else:
                self._stop()
        elif suffix == "ventilating/set":
            self._expect_bool(value)
            if not value:
                self._stop()
            elif phase != "off":
                raise Rejected(f"cannot ventilate from phase {phase}")
            else:
                level = settings.power_level
                self._send_pending(
                    lambda: self._send(MSG_VENTILATE, bytes([0xFF, 0xFF, level, 0xFF])),
                    lambda s, _st: self._phase(s) != "off",
                    name="ventilating",
                )
        elif suffix == "clear_error/set":
            if value is not True:
                raise Rejected("only true is accepted")
            if status.error_code == 0:
                raise Rejected("heater reports no error")
            self._send_pending(
                lambda: self._send(MSG_UNBLOCK), lambda s, _st: s.error_code == 0, name="clear_error"
            )
        else:
            self._settings_command(suffix, value, phase, ventilating, settings)

    @staticmethod
    def _expect_bool(value: object) -> None:
        if not isinstance(value, bool):
            raise Rejected(f"expected true or false, got {value!r}")

    def _start(self, status: Status, phase: str, ventilating: bool, settings: Settings) -> None:
        if ventilating or phase == "shutting_down":
            raise Rejected(f"cannot start while {'ventilating' if ventilating else phase}")
        if phase != "off":
            logger.info("Start ignored: heater already %s", phase)
            return
        if status.error_code != 0:
            raise Rejected(f"heater reports error {status.error_code}")
        self._send_pending(
            lambda: self._send(MSG_START, encode_settings(settings)),
            lambda s, _st: self._phase(s) != "off",
            name="start",
        )

    def _stop(self) -> None:
        self._stop_pending = True
        self._stop_attempts = 1
        self._stop_polls = 0
        self._pending = None
        self._send(MSG_STOP)

    def _settings_command(
        self, suffix: str, value: object, phase: str, ventilating: bool, settings: Settings
    ) -> None:
        if suffix == "mode/set":
            if not isinstance(value, str) or value not in CODE_BY_MODE:
                raise Rejected(f"unknown mode {value!r}")
            new = Settings(CODE_BY_MODE[value], settings.setpoint_c, settings.power_level)
        elif suffix == "setpoint_c/set":
            self._expect_int(value, 1, 30)
            new = Settings(settings.mode_code, value, settings.power_level)
        elif suffix == "power_level/set":
            self._expect_int(value, 0, 9)
            new = Settings(settings.mode_code, settings.setpoint_c, value)
        else:
            raise Rejected("unknown command topic")
        if ventilating or phase == "shutting_down":
            blocker = "ventilating" if ventilating else phase
            raise Rejected(f"settings cannot change while {blocker}")
        self._send_pending(
            lambda: self._send(MSG_SETTINGS, encode_settings(new)),
            lambda _s, st: st == new,
            reread_settings=True,
            name=suffix.removesuffix("/set"),
        )

    @staticmethod
    def _expect_int(value: object, low: int, high: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise Rejected(f"expected an integer, got {value!r}")
        if not low <= value <= high:
            raise Rejected(f"{value} outside {low}-{high}")

    # --- sending, retry and confirmation ----------------------------------------------
    def _send(self, msg: int, payload: bytes = b"") -> None:
        self._ensure_link().transact(msg, payload)

    def _read_settings(self) -> None:
        reply = self._ensure_link().transact(MSG_SETTINGS)
        settings = parse_settings(reply.payload) if reply else None
        if settings is not None:
            self._settings = settings

    def _send_pending(
        self,
        send: Callable[[], None],
        confirmed: Callable[[Status, Settings | None], bool],
        reread_settings: bool = False,
        name: str = "command",
    ) -> None:
        self._stop_pending = False
        send()
        self._read_settings()
        assert self._status is not None
        if reread_settings:
            # A settings write is confirmed by the very next 0x02 read: no retry.
            if not confirmed(self._status, self._settings):
                self._warn_unconfirmed(name)
        else:
            self._pending = _Pending(send, confirmed, reread_settings, name)
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
        if pending.reread_settings:
            self._read_settings()
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

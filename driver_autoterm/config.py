"""Layered INI config: config.default.ini (shipped) + config.ini (local
override).

Same two-file pattern as every renewvan/driver-* repo (see
driver-tank/driver_tank/config.py): config.default.ini ships every key with a
safe default and is never edited at an install; config.ini is a gitignored,
install-local file that overrides only what differs.
"""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from pathlib import Path

from driver_autoterm.protocol import MSG_SETTINGS, MSG_START, MSG_VENTILATE

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.default.ini"

# A clear_error frame may never be one that lights the burner or rewrites the settings: a capture
# that mis-attributes a start to the panel's "clear" must stop the Driver, not reach the heater.
_CLEAR_ERROR_FORBIDDEN_MSGS = frozenset({MSG_START, MSG_SETTINGS, MSG_VENTILATE})


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    client_id: str
    reconnect_min_delay: float
    reconnect_max_delay: float


@dataclass(frozen=True)
class DeviceConfig:
    id: str  # topic path segment, e.g. renewvan/heater/<id>/...
    name: str  # empty = not published
    update_interval_ms: int
    serial_device: str
    reply_timeout_s: float
    commands_enabled: bool
    stale_after_s: float
    send_init: bool
    # (msg, payload) sent for `clear_error/set`, from a bench capture; None refuses the command.
    clear_error_frame: tuple[int, bytes] | None = None


def parse_clear_error_frame(text: str) -> tuple[int, bytes] | None:
    """`"0d"` or `"0d 00 01"` (message byte, then payload, in hex) -> (msg, payload); empty -> None.

    Raises ValueError on bad hex or a forbidden message: better a stopped Driver than a guess."""
    text = text.strip()
    if not text:
        return None
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise ValueError(f"clear_error_frame is not hex bytes: {text!r}") from None
    msg, payload = raw[0], raw[1:]
    if msg in _CLEAR_ERROR_FORBIDDEN_MSGS:
        raise ValueError(
            f"clear_error_frame message 0x{msg:02x} starts the heater or writes its settings"
        )
    if len(payload) > 32:
        raise ValueError(f"clear_error_frame payload is {len(payload)} bytes (at most 32)")
    return msg, payload


@dataclass(frozen=True)
class AppConfig:
    mqtt: MqttConfig
    device: DeviceConfig


def load_config(
    default_path: Path = DEFAULT_CONFIG_PATH, local_path: Path | None = None
) -> AppConfig:
    """Load config.default.ini, then layer config.ini (if present) on top."""
    parser = configparser.ConfigParser()
    parser.read(default_path)
    if local_path is not None and local_path.exists():
        parser.read(local_path)
    else:
        logger.info("No local config override at %s; using defaults only", local_path)

    mqtt = parser["mqtt"]
    device = parser["device"]

    config = AppConfig(
        mqtt=MqttConfig(
            host=mqtt.get("host", "localhost"),
            port=mqtt.getint("port", 1883),
            username=mqtt.get("username") or None,
            password=mqtt.get("password") or None,
            client_id=mqtt.get("client_id", "driver-autoterm"),
            reconnect_min_delay=mqtt.getfloat("reconnect_min_delay", 1.0),
            reconnect_max_delay=mqtt.getfloat("reconnect_max_delay", 30.0),
        ),
        device=DeviceConfig(
            id=device.get("id", "air2d"),
            name=device.get("name", "Heater"),
            update_interval_ms=device.getint("update_interval_ms", 1000),
            serial_device=device.get("serial_device", "/dev/ttyAUTOTERM"),
            reply_timeout_s=device.getfloat("reply_timeout_s", 1.0),
            # getboolean raises on a typo: better a stopped Driver than a guess.
            commands_enabled=device.getboolean("commands_enabled", False),
            stale_after_s=device.getfloat("stale_after_s", 5.0),
            send_init=device.getboolean("send_init", False),
            clear_error_frame=parse_clear_error_frame(device.get("clear_error_frame", "")),
        ),
    )
    _validate(config.device)
    return config


def _validate(device: DeviceConfig) -> None:
    if device.stale_after_s * 1000 < 3 * device.update_interval_ms:
        raise ValueError(
            "stale_after_s must be at least 3 x update_interval_ms "
            f"({device.stale_after_s} s vs {device.update_interval_ms} ms)"
        )
    if device.reply_timeout_s * 1000 > device.update_interval_ms:
        raise ValueError(
            "reply_timeout_s must not exceed update_interval_ms "
            f"({device.reply_timeout_s} s vs {device.update_interval_ms} ms)"
        )
    if device.update_interval_ms > 2000:
        logger.warning(
            "update_interval_ms is %s: the heater may raise a no-communication fault "
            "when it is polled less often than every 2 s",
            device.update_interval_ms,
        )

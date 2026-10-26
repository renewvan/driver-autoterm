"""Autoterm 0xAA frame, CRC16-Modbus and payload codecs. Pure, no I/O.

Frame: AA <device> <payload_len> 00 <msg> <payload> <crc_hi> <crc_lo>, the CRC
covering everything before it. Requests use device 0x03, replies 0x04 (init and
unblock acks use 0x00). See .scratch/driver-heater/research in the hub repo.
"""

from __future__ import annotations

from dataclasses import dataclass

START = 0xAA
DEVICE_REQUEST = 0x03

MSG_START = 0x01
MSG_SETTINGS = 0x02
MSG_STOP = 0x03
MSG_SERIAL = 0x04
MSG_VERSION = 0x06
MSG_UNBLOCK = 0x0D
MSG_STATUS = 0x0F
MSG_INIT = 0x1C
MSG_VENTILATE = 0x23

STATUS_LEN = 19
EXTERNAL_ABSENT = 0x7F

# Mode byte on the wire; controller_temperature (2) is never published or sent.
MODE_BY_CODE = {1: "heater_temperature", 3: "external_temperature", 4: "power"}
CODE_BY_MODE = {name: code for code, name in MODE_BY_CODE.items()}


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


@dataclass(frozen=True)
class Frame:
    device: int
    msg: int
    payload: bytes = b""


def build_frame(msg: int, payload: bytes = b"", device: int = DEVICE_REQUEST) -> bytes:
    body = bytes([START, device, len(payload), 0x00, msg]) + payload
    crc = crc16_modbus(body)
    return body + bytes([crc >> 8, crc & 0xFF])


class FrameParser:
    """Reassembles frames from arbitrary chunks; on a bad frame drops one byte and rescans."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            start = self._buf.find(START)
            if start < 0:
                self._buf.clear()
                return frames
            del self._buf[:start]
            if len(self._buf) < 5:
                return frames
            total = self._buf[2] + 7
            if len(self._buf) < total:
                return frames
            raw = bytes(self._buf[:total])
            crc = crc16_modbus(raw[:-2])
            if self._buf[3] == 0x00 and raw[-2:] == bytes([crc >> 8, crc & 0xFF]):
                frames.append(Frame(device=raw[1], msg=raw[4], payload=raw[5:-2]))
                del self._buf[:total]
            else:
                del self._buf[:1]


@dataclass(frozen=True)
class Status:
    major: int
    minor: int
    error_code: int
    internal_temperature_c: int
    external_temperature_c: int | None
    voltage_v: float
    heater_temperature_c: int
    fan_rpm: int
    fuel_pump_hz: float

    @property
    def status_code(self) -> str:
        return f"{self.major}.{self.minor}"


def _signed(byte: int) -> int:
    return byte - 256 if byte > 127 else byte


def parse_status(payload: bytes) -> Status | None:
    """Decode the 19-byte AIR 2D status; any other length (e.g. the 4D's 10 bytes) is None."""
    if len(payload) != STATUS_LEN:
        return None
    return Status(
        major=payload[0],
        minor=payload[1],
        error_code=payload[2],
        internal_temperature_c=_signed(payload[3]),
        external_temperature_c=(None if payload[4] == EXTERNAL_ABSENT else _signed(payload[4])),
        voltage_v=round(payload[6] / 10, 1),
        heater_temperature_c=payload[8] - 15,
        fan_rpm=payload[12] * 60,
        fuel_pump_hz=round(payload[14] / 100, 2),
    )


@dataclass(frozen=True)
class Settings:
    mode_code: int
    setpoint_c: int
    power_level: int

    @property
    def mode(self) -> str | None:
        return MODE_BY_CODE.get(self.mode_code)


def parse_settings(payload: bytes) -> Settings | None:
    if len(payload) != 6:
        return None
    return Settings(mode_code=payload[2], setpoint_c=payload[3], power_level=payload[5])


def encode_settings(settings: Settings) -> bytes:
    """use_work_time = no (1), work_time = 0, wait_mode = 0."""
    return bytes([0x01, 0x00, settings.mode_code, settings.setpoint_c, 0x00, settings.power_level])

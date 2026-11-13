"""A fake AIR 2D on the far end of a serial stream, plus an in-memory MQTT publisher.

The heater models only what the research supports: replies echo/report, no NACK
(a frame it dislikes gets no reply), phases change when commands land.
"""

from __future__ import annotations

import time

from driver_autoterm.protocol import (
    MSG_INIT,
    MSG_SERIAL,
    MSG_SETTINGS,
    MSG_START,
    MSG_STATUS,
    MSG_STOP,
    MSG_UNBLOCK,
    MSG_VENTILATE,
    MSG_VERSION,
    FrameParser,
    build_frame,
)

REPLY = 0x04


class FakeHeater:
    def __init__(self) -> None:
        self.status = [0, 1]  # major, minor
        self.error = 0
        self.internal = 19
        self.external = 0x7F
        self.voltage_byte = 134
        self.heater_byte = 36
        self.fan_byte = 0
        self.pump_byte = 0
        self.settings = bytes([1, 0, 4, 16, 0, 8])
        self.status_payload_len10 = False
        self.silent = False  # unplugged: nothing answers
        self.ignore: dict[int, int] = {}  # msg -> number of upcoming frames to swallow
        self.received: list[tuple[int, bytes]] = []
        self.stop_effective = True
        self.drop_settings_writes = 0  # upcoming 0x02 writes (not reads) to swallow
        self.ventilate_status: list[int] | None = [3, 35]  # None: the frame changes nothing
        self._parser = FrameParser()
        self._out = bytearray()

    # --- serial stream interface -------------------------------------------------
    def write(self, data: bytes) -> None:
        for frame in self._parser.feed(data):
            self.received.append((frame.msg, frame.payload))
            if self.silent:
                continue
            if frame.msg == MSG_SETTINGS and frame.payload and self.drop_settings_writes > 0:
                self.drop_settings_writes -= 1
                continue
            if self.ignore.get(frame.msg, 0) > 0:
                self.ignore[frame.msg] -= 1
                continue
            self._respond(frame.msg, frame.payload)

    def read(self, size: int) -> bytes:
        if not self._out:
            time.sleep(0.001)
            return b""
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        pass

    # --- behaviour -----------------------------------------------------------------
    def _reply(self, msg: int, payload: bytes = b"", device: int = REPLY) -> None:
        self._out.extend(build_frame(msg, payload, device=device))

    def _respond(self, msg: int, payload: bytes) -> None:
        if msg == MSG_INIT:
            self._reply(msg, device=0x00)
        elif msg == MSG_SERIAL:
            self._reply(msg, bytes.fromhex("129e001580"))
        elif msg == MSG_VERSION:
            self._reply(msg, bytes.fromhex("0201030403"))
        elif msg == MSG_SETTINGS:
            if payload:
                self.settings = payload
            self._reply(msg, self.settings)
        elif msg == MSG_START:
            self.settings = payload
            self.status = [2, 1]
            self._reply(msg, payload)
        elif msg == MSG_STOP:
            if self.stop_effective and self.status[0] in (1, 2, 3):
                self.status = [3, 4]
            self._reply(msg)
        elif msg == MSG_VENTILATE:
            if self.ventilate_status is not None:
                self.status = list(self.ventilate_status)
            self._reply(msg, bytes([0xFF, 0xFF, payload[2], 0x43]))
        elif msg == MSG_UNBLOCK:
            self.error = 0
            self._reply(msg, device=0x00)
        elif msg == MSG_STATUS:
            self._reply(msg, self._status_payload())

    def _status_payload(self) -> bytes:
        if self.status_payload_len10:
            return bytes([0, 1, self.error, 29, 0x7F, 0, 125, 1, 65, 0])
        p = bytearray(19)
        p[0], p[1] = self.status
        p[2] = self.error
        p[3] = self.internal
        p[4] = self.external
        p[6] = self.voltage_byte
        p[8] = self.heater_byte
        p[11] = p[12] = self.fan_byte
        p[14] = self.pump_byte
        return bytes(p)

    def sent(self, msg: int) -> list[bytes]:
        return [p for m, p in self.received if m == msg]


class FakePublisher:
    """Retained-topic view of what the Driver published, and a way to send it commands."""

    def __init__(self) -> None:
        self.retained: dict[str, str] = {}
        self.subscriptions: dict[str, object] = {}

    def publish(self, topic: str, payload: str) -> None:
        self.retained[topic] = payload

    def subscribe(self, topic: str, callback) -> None:  # noqa: ANN001
        self.subscriptions[topic] = callback

    def send(self, topic: str, payload: str) -> None:
        self.subscriptions[topic](payload)

"""Request/reply over the heater's UART. One request in flight, no NACK: no reply is a timeout."""

from __future__ import annotations

import logging
import time
from typing import Protocol

from driver_autoterm.protocol import Frame, FrameParser, build_frame

logger = logging.getLogger(__name__)

BAUD_RATE = 9600  # 8N1, fixed by the heater


class Stream(Protocol):
    def write(self, data: bytes) -> object: ...
    def read(self, size: int) -> bytes: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


def open_serial(device: str) -> Stream:
    import serial

    return serial.Serial(device, BAUD_RATE, timeout=0.05)


class HeaterLink:
    def __init__(self, stream: Stream, reply_timeout_s: float) -> None:
        self._stream = stream
        self._timeout = reply_timeout_s
        self._parser = FrameParser()

    def transact(self, msg: int, payload: bytes = b"") -> Frame | None:
        """Send one request; the reply frame for `msg`, or None on timeout."""
        self._stream.reset_input_buffer()
        self._parser = FrameParser()
        self._stream.write(build_frame(msg, payload))
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            for frame in self._parser.feed(self._stream.read(64)):
                if frame.msg == msg and frame.device != 0x03:
                    return frame
        return None

    def close(self) -> None:
        self._stream.close()

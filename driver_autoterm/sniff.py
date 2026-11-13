"""Read-only logger for the heater's UART: it never writes, so it adds nothing to the bus.

Run it with the Driver stopped (a serial port has one opener) and the heater off, for a short
capture such as the heater's own panel clearing a fault (docs/bench-clear-error.md):

    python -m driver_autoterm.sniff --device /dev/ttyAUTOTERM --seconds 120

Every frame is printed with the wall-clock time, so it can be matched to a button press, and every
frame that is not a routine status or settings poll is marked. The run ends with a count per
distinct frame, rarest first: a frame seen once at the moment of the press is the candidate.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime

from driver_autoterm.protocol import (
    DEVICE_REQUEST,
    MSG_INIT,
    MSG_SERIAL,
    MSG_SETTINGS,
    MSG_START,
    MSG_STATUS,
    MSG_STOP,
    MSG_UNBLOCK,
    MSG_VENTILATE,
    MSG_VERSION,
    Frame,
    FrameParser,
)

BAUD_RATE = 9600  # 8N1, as the Driver uses
MSG_NAMES = {
    MSG_START: "start",
    MSG_SETTINGS: "settings",
    MSG_STOP: "stop",
    MSG_SERIAL: "serial",
    MSG_VERSION: "version",
    MSG_UNBLOCK: "unblock",
    MSG_STATUS: "status",
    MSG_INIT: "init",
    MSG_VENTILATE: "ventilate",
}
DEVICE_NAMES = {0x03: "request", 0x04: "reply", 0x00: "ack"}


def is_routine(frame: Frame) -> bool:
    """A status poll or a settings read (and their replies); a settings *write* is not routine."""
    if frame.msg == MSG_STATUS:
        return True
    return frame.msg == MSG_SETTINGS and not (frame.device == DEVICE_REQUEST and frame.payload)


def describe(frame: Frame) -> str:
    who = DEVICE_NAMES.get(frame.device, f"dev {frame.device:02x}")
    name = MSG_NAMES.get(frame.msg, "unknown")
    mark = "" if is_routine(frame) else "   <-- not a poll"
    return f"{who:<7} msg {frame.msg:02x} {name:<9} payload [{frame.payload.hex(' ')}]{mark}"


def summarize(frames: Iterable[Frame]) -> list[str]:
    """One line per distinct frame, non-routine first, then rarest first."""
    counts = Counter(frames)
    ordered = sorted(counts.items(), key=lambda item: (is_routine(item[0]), item[1]))
    return [f"{count:>5} x  {describe(frame)}" for frame, count in ordered]


def frame_bytes(frame: Frame) -> int:
    return len(frame.payload) + 7  # AA, device, length, 00, msg, payload, 2 CRC bytes


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", default="/dev/ttyAUTOTERM")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--raw", action="store_true", help="also print every chunk as read")
    args = parser.parse_args(argv)

    import serial

    port = serial.Serial(args.device, BAUD_RATE, timeout=0.05)  # opened for reading only
    frames: list[Frame] = []
    frame_parser = FrameParser()
    read_total = 0
    start = time.monotonic()
    print(f"Listening on {args.device} for {args.seconds:g} s (read-only)", flush=True)
    try:
        while time.monotonic() - start < args.seconds:
            data = port.read(64)
            if not data:
                continue
            read_total += len(data)
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            if args.raw:
                print(f"{stamp} raw {data.hex(' ')}", flush=True)
            for frame in frame_parser.feed(data):
                frames.append(frame)
                print(f"{stamp} {describe(frame)}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        port.close()

    print(f"\n{len(frames)} frames, {read_total} bytes read", flush=True)
    unparsed = read_total - sum(frame_bytes(f) for f in frames)
    if unparsed > 0:
        print(f"{unparsed} bytes did not parse as frames (rerun with --raw to see them)")
    if not frames:
        print("No frames: nothing is talking on this line, or the panel is wired elsewhere.")
    for line in summarize(frames):
        print(line)


if __name__ == "__main__":
    main()

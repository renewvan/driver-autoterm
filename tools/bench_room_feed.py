"""Bench only: run the heater in mode 2 (panel sensor) while feeding it chosen 0x11 readings.

Not part of the Driver and not shipped in the image. It stands in for the panel for one run, with
the Driver stopped and the panel unplugged, to answer ADR 0031's bench questions: does the heater
regulate on the 0x11 value, and what does it do when the readings stop?

    python bench_room_feed.py --setpoint 20 --plan 10:420,28:420

The plan is a list of `value:seconds` steps, where value is the °C sent in 0x11 or `none` for no
feed. The heater is started --start-after seconds into the plan (with mode 2 and the setpoint),
only from status 0.x with error 0. It polls status every second throughout, as the panel does.
At the end of the plan, on Ctrl-C or on any error, it sends a stop (up to 3 times, 3 s apart) and
keeps polling until the heater is back to 0.x, so the heater is never left unpolled while it runs.
A second Ctrl-C exits at once; the heater then stops itself with error 30 after a few minutes.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import replace

from driver_autoterm.link import HeaterLink, open_serial
from driver_autoterm.protocol import (
    MSG_SETTINGS,
    MSG_START,
    MSG_STATUS,
    MSG_STOP,
    encode_settings,
    parse_settings,
    parse_status,
)

MSG_PANEL_TEMPERATURE = 0x11
MODE_PANEL_SENSOR = 2
STOP_TRIES = 3
STOP_SPACING_S = 3.0
WIND_DOWN_LIMIT_S = 420.0

Plan = list[tuple[int | None, float]]


def parse_plan(text: str) -> Plan:
    """`10:420,none:600` -> [(10, 420.0), (None, 600.0)]. Values -30..50 °C, durations > 0."""
    plan: Plan = []
    for step in text.split(","):
        value_text, _, seconds_text = step.strip().partition(":")
        seconds = float(seconds_text)
        if seconds <= 0:
            raise ValueError(f"step {step!r}: duration must be positive")
        if value_text.lower() == "none":
            plan.append((None, seconds))
            continue
        value = int(value_text)
        if not -30 <= value <= 50:
            raise ValueError(f"step {step!r}: {value} °C is outside -30..50")
        plan.append((value, seconds))
    if not plan:
        raise ValueError("empty plan")
    return plan


def step_at(plan: Plan, elapsed_s: float) -> tuple[bool, int | None]:
    """(still in the plan, value to send) at `elapsed_s` from the plan's start."""
    end = 0.0
    for value, seconds in plan:
        end += seconds
        if elapsed_s < end:
            return True, value
    return False, None


def temperature_byte(value: int) -> bytes:
    """The 0x11 payload: one signed byte, °C."""
    return bytes([value & 0xFF])


class Bench:
    def __init__(self, link: HeaterLink) -> None:
        self.link = link
        self.status = None
        self.stop_requested = False

    def poll(self, elapsed_s: float, feed: int | None, read_settings: bool) -> None:
        reply = self.link.transact(MSG_STATUS)
        status = parse_status(reply.payload) if reply else None
        if status is not None:
            self.status = status
        echo = "-"
        if feed is not None:
            ack = self.link.transact(MSG_PANEL_TEMPERATURE, temperature_byte(feed))
            echo = ack.payload.hex() if ack else "no echo"
        shown = "none" if feed is None else str(feed)
        line = f"{elapsed_s:7.0f}s feed={shown:>4} echo={echo:>7} "
        if status is None:
            line += "STATUS: no reply"
        else:
            line += (
                f"status={status.status_code:>4} err={status.error_code} "
                f"room={status.internal_temperature_c} heater={status.heater_temperature_c} "
                f"fan={status.fan_rpm} pump={status.fuel_pump_hz}"
            )
        if read_settings:
            settings_reply = self.link.transact(MSG_SETTINGS)
            line += f" 0x02={settings_reply.payload.hex(' ') if settings_reply else 'no reply'}"
        print(line, flush=True)

    def start(self, setpoint_c: int) -> bool:
        if self.status is None or self.status.major != 0 or self.status.error_code != 0:
            print("NOT STARTING: the heater is not off with error 0", flush=True)
            return False
        reply = self.link.transact(MSG_SETTINGS)
        settings = parse_settings(reply.payload) if reply else None
        if settings is None:
            print("NOT STARTING: no settings reply", flush=True)
            return False
        payload = encode_settings(
            replace(settings, mode_code=MODE_PANEL_SENSOR, setpoint_c=setpoint_c)
        )
        ack = self.link.transact(MSG_START, payload)
        print(f"START sent {payload.hex(' ')}: {'acked' if ack else 'no ack'}", flush=True)
        return True

    def wind_down(self, clock: float) -> None:
        """Stop (up to 3 tries) and keep polling until 0.x, or give up after 7 minutes."""
        began = time.monotonic()
        tries = 0
        last_try = 0.0
        while time.monotonic() - began < WIND_DOWN_LIMIT_S:
            self.poll(time.monotonic() - clock, None, read_settings=False)
            if self.status is not None and self.status.major == 0:
                code, error = self.status.status_code, self.status.error_code
                print(f"Heater off ({code}), error {error}.", flush=True)
                return
            in_shutdown = self.status is not None and self.status.major == 4
            due = time.monotonic() - last_try >= STOP_SPACING_S
            if not in_shutdown and tries < STOP_TRIES and due:
                ack = self.link.transact(MSG_STOP)
                tries += 1
                last_try = time.monotonic()
                print(f"STOP {tries}/{STOP_TRIES}: {'acked' if ack else 'no ack'}", flush=True)
            time.sleep(1.0)
        print("WARNING: heater not back to 0.x after 7 minutes; keep watching it.", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bench: mode 2 run with chosen 0x11 readings.")
    parser.add_argument("--device", default="/dev/ttyAUTOTERM")
    parser.add_argument("--setpoint", type=int, required=True, help="°C, 1-30")
    parser.add_argument("--plan", required=True, help="value:seconds,... e.g. 10:420,none:900")
    parser.add_argument("--start-after", type=float, default=20.0, help="seconds into the plan")
    args = parser.parse_args(argv)
    if not 1 <= args.setpoint <= 30:
        parser.error("--setpoint must be 1-30")
    plan = parse_plan(args.plan)

    bench = Bench(HeaterLink(open_serial(args.device), reply_timeout_s=1.0))

    def on_signal(signum: int, _frame: object) -> None:
        if bench.stop_requested:
            print("Second interrupt: exiting without waiting.", flush=True)
            sys.exit(1)
        bench.stop_requested = True
        print(f"Signal {signum}: stopping the heater (Ctrl-C again to exit at once).", flush=True)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    clock = time.monotonic()
    started = False
    tick = 0
    try:
        while not bench.stop_requested:
            elapsed = time.monotonic() - clock
            in_plan, feed = step_at(plan, elapsed)
            if not in_plan:
                print("Plan finished.", flush=True)
                break
            bench.poll(elapsed, feed, read_settings=tick % 5 == 0)
            if not started and elapsed >= args.start_after:
                started = True
                if not bench.start(args.setpoint):
                    break
            tick += 1
            time.sleep(max(0.0, 1.0 - (time.monotonic() - clock - elapsed)))
    finally:
        bench.wind_down(clock)
    return 0


if __name__ == "__main__":
    sys.exit(main())

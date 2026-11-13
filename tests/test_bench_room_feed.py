"""The bench-only room-feed tool (tools/bench_room_feed.py): its plan logic and its safety exits."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from driver_autoterm.protocol import MSG_SETTINGS, MSG_START, MSG_STATUS, MSG_STOP, Frame

_PATH = Path(__file__).resolve().parents[1] / "tools" / "bench_room_feed.py"
_SPEC = importlib.util.spec_from_file_location("bench_room_feed", _PATH)
bench_room_feed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench_room_feed)


def status_payload(major: int, minor: int, error: int = 0) -> bytes:
    payload = bytearray(19)
    payload[0], payload[1], payload[2] = major, minor, error
    payload[4] = 0x7F
    return bytes(payload)


class FakeLink:
    """Answers status with a scripted sequence of (major, minor, error); records what was sent."""

    def __init__(self, statuses: list[tuple[int, int, int]]) -> None:
        self.statuses = statuses
        self.sent: list[tuple[int, bytes]] = []

    def transact(self, msg: int, payload: bytes = b"") -> Frame | None:
        self.sent.append((msg, payload))
        if msg == MSG_STATUS:
            major, minor, error = (
                self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            )
            return Frame(device=0x04, msg=msg, payload=status_payload(major, minor, error))
        if msg == MSG_SETTINGS:
            return Frame(device=0x04, msg=msg, payload=bytes([0x01, 0x00, 0x01, 0x14, 0x01, 0x05]))
        return Frame(device=0x04, msg=msg, payload=payload)

    def msgs(self, msg: int) -> list[bytes]:
        return [payload for sent, payload in self.sent if sent == msg]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(bench_room_feed.time, "sleep", lambda _s: None)


def test_parse_plan_reads_values_and_none():
    assert bench_room_feed.parse_plan("10:420, none:600,-5:30") == [
        (10, 420.0),
        (None, 600.0),
        (-5, 30.0),
    ]


@pytest.mark.parametrize("plan", ["", "10:0", "60:30", "-31:30", "ten:30"])
def test_parse_plan_rejects_bad_steps(plan):
    with pytest.raises(ValueError):
        bench_room_feed.parse_plan(plan)


def test_step_at_walks_the_plan_and_ends():
    plan = [(10, 60.0), (None, 30.0)]
    assert bench_room_feed.step_at(plan, 0) == (True, 10)
    assert bench_room_feed.step_at(plan, 60) == (True, None)
    assert bench_room_feed.step_at(plan, 90) == (False, None)


def test_temperature_byte_is_signed():
    assert bench_room_feed.temperature_byte(20) == b"\x14"
    assert bench_room_feed.temperature_byte(-5) == b"\xfb"


def test_start_is_refused_with_an_error_code():
    link = FakeLink([(0, 1, 30)])
    bench = bench_room_feed.Bench(link)
    bench.poll(0, 10, read_settings=False)
    assert bench.start(20) is False
    assert link.msgs(MSG_START) == []


def test_start_sends_mode_2_and_the_setpoint_keeping_the_other_bytes():
    link = FakeLink([(0, 1, 0)])
    bench = bench_room_feed.Bench(link)
    bench.poll(0, 10, read_settings=False)
    assert bench.start(18) is True
    assert link.msgs(MSG_START) == [bytes([0x01, 0x00, 0x02, 0x12, 0x01, 0x05])]


def test_feed_sends_0x11_only_with_a_value():
    link = FakeLink([(0, 1, 0)])
    bench = bench_room_feed.Bench(link)
    bench.poll(0, 10, read_settings=False)
    bench.poll(1, None, read_settings=False)
    assert link.msgs(0x11) == [b"\x0a"]


def test_wind_down_stops_a_running_heater_and_polls_until_off():
    link = FakeLink([(3, 0, 0), (3, 0, 0), (4, 0, 0), (4, 0, 0), (0, 1, 0)])
    bench_room_feed.Bench(link).wind_down(clock=0.0)
    assert len(link.msgs(MSG_STOP)) == 1
    assert len(link.msgs(MSG_STATUS)) == 5


def test_wind_down_sends_nothing_when_already_off():
    link = FakeLink([(0, 1, 0)])
    bench_room_feed.Bench(link).wind_down(clock=0.0)
    assert link.msgs(MSG_STOP) == []

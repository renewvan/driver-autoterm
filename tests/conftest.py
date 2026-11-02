import json
import logging

import pytest

from driver_autoterm.config import DeviceConfig
from driver_autoterm.driver import HeaterDriver
from tests.fake_heater import FakeHeater, FakePublisher


class Rig:
    def __init__(self, **overrides) -> None:
        fields = dict(
            id="air2d",
            name="Heater",
            update_interval_ms=1000,
            serial_device="/dev/null",
            reply_timeout_s=0.02,
            commands_enabled=True,
            stale_after_s=5.0,
            send_init=True,
        )
        fields.update(overrides)
        self.cfg = DeviceConfig(**fields)
        self.heater = FakeHeater()
        self.pub = FakePublisher()
        self.now = 100.0
        self.driver = HeaterDriver(self.cfg, self.pub, lambda: self.heater, lambda: self.now)
        self.driver.start()

    def poll(self, n: int = 1) -> None:
        for _ in range(n):
            self.now += 1.0
            self.driver.poll()

    def value(self, name: str):
        raw = self.pub.retained[f"renewvan/heater/air2d/{name}"]
        return None if raw == "" else json.loads(raw)

    def command(self, suffix: str, payload, raw: bool = False) -> None:
        text = payload if raw else json.dumps(payload)
        self.pub.send(f"renewvan/heater/air2d/{suffix}", text)

    def set_status(self, major: int, minor: int, error: int = 0) -> None:
        self.heater.status = [major, minor]
        self.heater.error = error

    def frames(self, msg: int) -> list[bytes]:
        return self.heater.sent(msg)


@pytest.fixture
def rig() -> Rig:
    r = Rig()
    r.poll()
    return r


@pytest.fixture
def make_rig():
    return Rig


@pytest.fixture
def warnings(caplog):
    caplog.set_level(logging.WARNING)
    return lambda: [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]

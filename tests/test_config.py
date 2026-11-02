from pathlib import Path

import pytest

from driver_autoterm.config import load_config


def test_load_config_defaults() -> None:
    config = load_config()
    assert config.mqtt.port == 1883
    assert config.device.id == "air2d"


def test_load_config_local_override(tmp_path: Path) -> None:
    local = tmp_path / "config.ini"
    local.write_text("[mqtt]\nhost = 192.168.1.50\n")
    config = load_config(local_path=local)
    assert config.mqtt.host == "192.168.1.50"


def _load(tmp_path: Path, body: str):
    local = tmp_path / "config.ini"
    local.write_text(body)
    return load_config(local_path=local)


def test_new_defaults() -> None:
    d = load_config().device
    assert (d.name, d.update_interval_ms, d.serial_device) == ("Heater", 1000, "/dev/ttyAUTOTERM")
    assert (d.commands_enabled, d.stale_after_s, d.send_init) == (False, 5.0, False)


def test_commands_enabled_typo_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _load(tmp_path, "[device]\ncommands_enabled = ture\n")


def test_commands_enabled_true(tmp_path: Path) -> None:
    assert _load(tmp_path, "[device]\ncommands_enabled = true\n").device.commands_enabled


def test_stale_after_must_cover_three_polls(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _load(tmp_path, "[device]\nstale_after_s = 2\n")


def test_reply_timeout_must_fit_the_poll_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _load(tmp_path, "[device]\nreply_timeout_s = 1.5\n")


def test_slow_poll_interval_only_warns(tmp_path: Path, caplog) -> None:
    body = "[device]\nupdate_interval_ms = 2500\nstale_after_s = 10\nreply_timeout_s = 1\n"
    config = _load(tmp_path, body)
    assert config.device.update_interval_ms == 2500
    assert "no-communication" in caplog.text


def test_send_init_can_be_enabled(tmp_path: Path) -> None:
    assert _load(tmp_path, "[device]\nsend_init = true\n").device.send_init is True

"""The MQTT session must be clean, so commands sent while the Driver was away are never replayed."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from driver_autoterm.config import MqttConfig
from driver_autoterm.publisher import Publisher

CONFIG = MqttConfig(
    host="localhost",
    port=1883,
    username=None,
    password=None,
    client_id="driver-autoterm",
    reconnect_min_delay=1.0,
    reconnect_max_delay=30.0,
)


def test_session_is_clean_so_the_broker_never_queues_commands_for_an_offline_driver():
    with patch("driver_autoterm.publisher.mqtt.Client") as client_cls:
        Publisher(CONFIG, info="{}")
    assert client_cls.call_args.kwargs["clean_session"] is True


def test_every_connect_resubscribes_since_a_clean_session_keeps_no_subscriptions():
    with patch("driver_autoterm.publisher.mqtt.Client"):
        publisher = Publisher(CONFIG, info="{}")
    publisher.subscribe("renewvan/heater/air2d/set", lambda payload: None)

    reconnected = MagicMock()
    publisher._on_connect(reconnected, None, {}, 0)

    reconnected.subscribe.assert_called_once_with("renewvan/heater/air2d/set", qos=1)

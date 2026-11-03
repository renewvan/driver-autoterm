"""Driver info/health live under the Driver key (autoterm), not the entity (heater)."""

from driver_autoterm.publisher import HEALTH_TOPIC, INFO_TOPIC


def test_driver_topics_use_driver_key():
    assert INFO_TOPIC == "renewvan/autoterm/info"
    assert HEALTH_TOPIC == "renewvan/autoterm/health"

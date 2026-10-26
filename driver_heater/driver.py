"""Publish loop: TODO describe Heater read -> publish ->
renewvan/heater/<id>/<property>.

This is a scaffolded stub -- see the design checklist in README.md. Fill
in, in order:

1. A reader for the physical hardware/feed (see driver-tank's adc.py for a
   device that owns the sensing, or driver-venus's Victron MQTT subscription
   for a device that remaps an existing feed -- Q1 of the design round
   decides which shape this is).
2. The fields this device publishes, matching a hub/schema/<entity>.schema.json
   contract (new entity) or an existing one (Q2/Q3 of the design round).
3. Identity fields (published once at startup) vs live fields (republished
   every poll) -- see driver-tank's publish_identity()/read_and_publish()
   split for the pattern.
"""

from __future__ import annotations

import logging
import time

from driver_heater.config import AppConfig
from driver_heater.publisher import Publisher

logger = logging.getLogger(__name__)


def _topic(device_id: str, prop: str) -> str:
    return f"renewvan/heater/{device_id}/{prop}"


def read_and_publish(publisher: Publisher, config: AppConfig) -> None:
    """TODO: read the hardware/feed and publish live fields via
    publisher.publish(_topic(config.device.id, "<property>"), payload).
    """
    raise NotImplementedError(
        "Implement the Heater read+publish step (see this module's docstring)"
    )


def run(config: AppConfig) -> None:
    publisher = Publisher(config.mqtt)
    publisher.connect()
    try:
        while True:
            read_and_publish(publisher, config)
            time.sleep(config.device.update_interval_ms / 1000)
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        publisher.disconnect()

"""Wire the MQTT transport to Home Assistant's own broker client.

The Home-Assistant-shaped half of the local MQTT transport, kept apart from
mqtt_api.py for the same reason ble_connect.py is kept apart from ble_link.py:
everything that talks protocol stays testable without Home Assistant, and
everything that talks to Home Assistant lives in one small file.

Import this lazily. The mqtt integration is a soft dependency, so an entry on
another transport must not pay for it.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components import mqtt
from homeassistant.core import callback

from .mqtt_api import EzhiMqttApi, SemMqttApi
from .ntp_responder import NtpResponder
from .sem_feed import SemFeed

_LOGGER = logging.getLogger(__name__)

# The device publishes at QoS 1 and so do we: a dropped command is worse than
# a repeated one here, and the envelope carries a correlation id, so a
# duplicate resolves the same future twice -- which the transport ignores.
QOS = 1

# mqtt.async_subscribe returns once the subscription is queued, not once the
# broker has it. At Home Assistant startup the client batches subscriptions
# behind a debouncer that every new subscription restarts -- with a couple of
# thousand discovered MQTT entities setting up at the same time, the SUBSCRIBE
# can leave many seconds later. The first poll's request went out at once, the
# device answered on its next 5 s tick, and the reply reached a broker that
# had nobody subscribed yet: "did not answer read systemMode within 12 s"
# after every restart, 5 of 5 between 2026-09-25 and 09-26, with the device
# connected throughout. So wait for the broker's acknowledgement, bounded --
# kept under the 20 s the startup arm gives the whole first refresh.
SUBACK_TIMEOUT = 15.0


async def _wait_for_suback(hass, topic: str) -> bool:
    """True once the broker has acknowledged `topic`, False after the timeout."""
    on_done = getattr(mqtt, "async_on_subscribe_done", None)
    if on_done is None:  # older core without the hook: behave as before
        return True
    loop = asyncio.get_running_loop()
    acked = asyncio.Event()
    stop = on_done(hass, topic, QOS, acked.set)
    started = loop.time()
    try:
        async with asyncio.timeout(SUBACK_TIMEOUT):
            await acked.wait()
        _LOGGER.debug("EZHI: broker acknowledged %s after %.1f s",
                      topic, loop.time() - started)
        return True
    except TimeoutError:
        # Carry on: the next poll finds the subscription in place. Say where
        # the delay sits, so nobody goes debugging the inverter for it.
        _LOGGER.warning(
            "EZHI: the broker did not acknowledge the subscription to %s "
            "within %.0f s; this is Home Assistant's MQTT client, not the "
            "inverter, and the first poll may miss its reply",
            topic, SUBACK_TIMEOUT)
        return False
    finally:
        stop()


def _broker_io(hass):
    """The publish/subscribe pair every object below is built on.

    One place, so the @callback rule below holds for all of them, and so does
    the wait for the broker's acknowledgement.
    """
    # One timeout per object, not one per topic: after a miss the client is
    # evidently still batching, and a second full wait would only hold the
    # local sensors' setup longer for nothing.
    wait_for_ack = True

    async def publish(topic: str, payload: str) -> None:
        await mqtt.async_publish(hass, topic, payload, qos=QOS)

    async def subscribe(topic: str, handler):
        # @callback is load-bearing, not decoration. Home Assistant infers the
        # job type from the function it is handed: a plain one becomes
        # HassJobType.Executor and is dispatched with run_in_executor. The
        # transport resolves asyncio Futures inside this handler, which is
        # only safe on the event loop -- off it, it races wait_for's
        # cancellation at the timeout boundary, and raises on every single
        # reply once the loop runs in debug mode.
        @callback
        def _forward(message) -> None:
            handler(message.payload)

        nonlocal wait_for_ack
        unsubscribe = await mqtt.async_subscribe(hass, topic, _forward, qos=QOS)
        if wait_for_ack:
            wait_for_ack = await _wait_for_suback(hass, topic)
        return unsubscribe

    return publish, subscribe


def make_mqtt_api(hass, device_id: str) -> EzhiMqttApi:
    """An EzhiMqttApi talking through Home Assistant's broker connection."""
    publish, subscribe = _broker_io(hass)
    return EzhiMqttApi(device_id, publish, subscribe)


def make_sem_api(hass, device_id: str) -> SemMqttApi:
    """A SemMqttApi (the smart meter's side of Local Control), same broker."""
    publish, subscribe = _broker_io(hass)
    return SemMqttApi(device_id, publish, subscribe)


def make_sem_feed(hass, device_id: str) -> SemFeed:
    """The smart meter's pushed readings, off Home Assistant's broker client."""
    _publish, subscribe = _broker_io(hass)
    return SemFeed(device_id, subscribe)


def make_ntp_responder(hass, targets, fallback_timezone) -> NtpResponder:
    """Answers the time requests of `targets`, an iterable of
    (product key, device id). `fallback_timezone` is called per request, so a
    change of the Home Assistant time zone is picked up without a reload."""
    publish, subscribe = _broker_io(hass)
    return NtpResponder(publish, subscribe, targets, fallback_timezone)

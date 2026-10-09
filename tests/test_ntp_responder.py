"""The optional time-of-day answerer for redirected devices.

Home-Assistant-free: the broker is a dict of topic -> handler, so a request is
"call the handler" and an answer is "something landed in `published`".
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from ezhi_component import mqtt_protocol as p
from ezhi_component.ntp_responder import NtpResponder

EZHI = "D00000000000"
SEM = "M00000000000"
NOW = datetime(2026, 10, 8, 11, 5, 9, tzinfo=timezone.utc)


class Broker:
    """Just enough broker: subscriptions in, publications out."""

    def __init__(self, fail_publish: bool = False, fail_subscribe_on: str | None = None):
        self.handlers: dict[str, object] = {}
        self.published: list[tuple[str, dict]] = []
        self.unsubscribed: list[str] = []
        self.fail_publish = fail_publish
        self.fail_subscribe_on = fail_subscribe_on

    async def publish(self, topic: str, payload: str) -> None:
        if self.fail_publish:
            raise OSError("broker gone")
        self.published.append((topic, json.loads(payload)))

    async def subscribe(self, topic: str, handler):
        if topic == self.fail_subscribe_on:
            raise OSError("subscribe refused")
        self.handlers[topic] = handler

        def unsubscribe():
            self.unsubscribed.append(topic)

        return unsubscribe

    def deliver(self, topic: str, payload) -> None:
        self.handlers[topic](payload)


def make(broker, targets=((p.PRODUCT_KEY, EZHI), (p.SEM_PRODUCT_KEY, SEM)), tz="Europe/Berlin"):
    return NtpResponder(broker.publish, broker.subscribe, targets, lambda: tz, lambda: NOW)


def request(corr_id="42", tz="Europe/Berlin") -> str:
    return json.dumps({"id": corr_id, "params": {"timezone": tz}})


def run(coro):
    return asyncio.run(coro)


def settle():
    """Let the tasks the handler spawned run."""
    return asyncio.sleep(0)


def test_it_listens_on_exactly_the_devices_it_was_told_about():
    async def scenario():
        broker = Broker()
        await make(broker).async_start()
        assert set(broker.handlers) == {
            f"/ntp/EZHI/{EZHI}/get",
            f"/ntp/SEM/{SEM}/get",
        }
        # never a wildcard: a shared broker has other people's devices on it
        assert not any("#" in t or "+" in t for t in broker.handlers)

    run(scenario())


def test_the_inverter_gets_its_own_zone_back():
    async def scenario():
        broker = Broker()
        responder = make(broker)
        await responder.async_start()
        broker.deliver(f"/ntp/EZHI/{EZHI}/get", request("7", "Europe/London"))
        await settle()
        (topic, body), = broker.published
        assert topic == f"/ntp/EZHI/{EZHI}/get_reply"
        assert body["id"] == "7"
        assert body["productKey"] == "EZHI"
        assert body["data"]["timezone"] == "Europe/London"
        assert body["data"]["date"] == "20261008110509"
        assert body["data"]["timeOffset"] == "3600000"      # London is on BST then
        assert body["code"] == 200
        assert responder.answered == 1

    run(scenario())


def test_the_meter_asks_for_no_zone_and_gets_the_fallback():
    async def scenario():
        broker = Broker()
        await make(broker, tz="Europe/Berlin").async_start()
        broker.deliver(f"/ntp/SEM/{SEM}/get", json.dumps({"id": "9", "params": {"timezone": ""}}))
        await settle()
        (topic, body), = broker.published
        assert topic == f"/ntp/SEM/{SEM}/get_reply"
        assert body["productKey"] == "SEM"
        assert body["data"]["timezone"] == "Europe/Berlin"
        assert body["data"]["timeOffset"] == "7200000"      # CEST

    run(scenario())


def test_the_fallback_is_looked_up_per_request():
    """A changed Home Assistant time zone must not need a reload."""
    async def scenario():
        broker = Broker()
        zone = ["Europe/Berlin"]
        responder = NtpResponder(
            broker.publish, broker.subscribe, [(p.SEM_PRODUCT_KEY, SEM)],
            lambda: zone[0], lambda: NOW)
        await responder.async_start()
        topic = f"/ntp/SEM/{SEM}/get"
        broker.deliver(topic, json.dumps({"id": "1"}))
        zone[0] = "America/New_York"
        broker.deliver(topic, json.dumps({"id": "2"}))
        await settle()
        zones = [body["data"]["timezone"] for _t, body in broker.published]
        assert zones == ["Europe/Berlin", "America/New_York"]

    run(scenario())


def test_junk_is_dropped_and_the_responder_stays_up():
    async def scenario():
        broker = Broker()
        responder = make(broker)
        await responder.async_start()
        topic = f"/ntp/EZHI/{EZHI}/get"
        for junk in ("not json", "[]", '{"params":{}}', b"\xff\xfe"):
            broker.deliver(topic, junk)
        await settle()
        assert broker.published == []
        broker.deliver(topic, request("ok"))
        await settle()
        assert [b["id"] for _t, b in broker.published] == ["ok"]

    run(scenario())


def test_a_failed_publish_is_not_counted_and_does_not_raise():
    async def scenario():
        broker = Broker(fail_publish=True)
        responder = make(broker)
        await responder.async_start()
        broker.deliver(f"/ntp/EZHI/{EZHI}/get", request())
        await settle()
        await settle()
        assert responder.answered == 0

    run(scenario())


def test_start_twice_subscribes_once():
    async def scenario():
        broker = Broker()
        responder = make(broker)
        await responder.async_start()
        first = dict(broker.handlers)
        await responder.async_start()
        assert broker.handlers == first
        await responder.async_stop()
        assert len(broker.unsubscribed) == 2

    run(scenario())


def test_stop_unsubscribes_everything():
    async def scenario():
        broker = Broker()
        responder = make(broker)
        await responder.async_start()
        await responder.async_stop()
        assert sorted(broker.unsubscribed) == sorted(broker.handlers)

    run(scenario())


def test_a_failed_second_subscription_undoes_the_first():
    async def scenario():
        broker = Broker(fail_subscribe_on=f"/ntp/SEM/{SEM}/get")
        responder = make(broker)
        try:
            await responder.async_start()
        except OSError:
            pass
        else:
            raise AssertionError("the subscribe error must reach the caller")
        assert broker.unsubscribed == [f"/ntp/EZHI/{EZHI}/get"]

    run(scenario())


def test_stop_survives_an_unsubscribe_that_raises():
    async def scenario():
        broker = Broker()

        async def subscribe(topic, handler):
            def unsubscribe():
                raise RuntimeError("already gone")
            return unsubscribe

        responder = NtpResponder(
            broker.publish, subscribe, [(p.PRODUCT_KEY, EZHI)], lambda: "UTC", lambda: NOW)
        await responder.async_start()
        await responder.async_stop()          # must not raise

    run(scenario())

"""The smart meter's pushed readings.

Payloads are shaped like the real events (scrubbed id, strings with four
decimals, cumulative energies alongside the power).
"""
from __future__ import annotations

import asyncio
import json

from ezhi_component.sem_feed import SemFeed

SEM = "M00000000000"
TOPIC = f"/event/SEM/{SEM}/post"


def event(**data) -> str:
    return json.dumps({"identifier": "outputDataSecond", "deviceId": SEM, "data": data})


FULL = dict(
    p="123.4500", p1="40.0000", p2="43.4500", p3="40.0000",
    iE="1000.1000", iE1="300.0000", iE2="400.0000", iE3="300.1000",
    eE="0.0140", eE1="0.0000", eE2="0.0140", eE3="0.0000",
)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Broker:
    def __init__(self):
        self.topic = None
        self.handler = None
        self.unsubscribed = 0

    async def subscribe(self, topic, handler):
        self.topic, self.handler = topic, handler

        def unsubscribe():
            self.unsubscribed += 1

        return unsubscribe


def started(min_interval=1.0):
    clock = Clock()
    broker = Broker()
    feed = SemFeed(SEM, broker.subscribe, min_interval=min_interval, clock=clock)
    asyncio.run(feed.async_start())
    return feed, broker, clock


def test_it_listens_on_the_meters_event_topic():
    feed, broker, _ = started()
    assert broker.topic == TOPIC
    assert feed.device_id == SEM


def test_nothing_is_known_before_the_first_event():
    feed, _b, _c = started()
    assert feed.latest is None and feed.age is None


def test_a_full_event_becomes_floats():
    feed, broker, _ = started()
    broker.handler(event(**FULL))
    assert feed.latest == {
        "p": 123.45, "p1": 40.0, "p2": 43.45, "p3": 40.0,
        "iE": 1000.1, "iE1": 300.0, "iE2": 400.0, "iE3": 300.1,
        "eE": 0.014, "eE1": 0.0, "eE2": 0.014, "eE3": 0.0,
    }


def test_a_negative_total_is_kept_as_it_is():
    """Negative would be export; the feed reports, it does not judge."""
    feed, broker, _ = started()
    broker.handler(event(p="-50.0000"))
    assert feed.latest["p"] == -50.0


def test_age_counts_from_the_last_event():
    feed, broker, clock = started()
    broker.handler(event(**FULL))
    clock.now += 7.5
    assert feed.age == 7.5


def test_other_events_are_ignored():
    feed, broker, _ = started()
    broker.handler(json.dumps({"identifier": "somethingElse", "data": {"p": "1"}}))
    assert feed.latest is None


def test_an_event_without_a_total_is_ignored():
    feed, broker, _ = started()
    broker.handler(event(p1="1.0000", p2="2.0000"))
    assert feed.latest is None


def test_a_trimmed_event_does_not_blank_the_energies():
    feed, broker, clock = started()
    broker.handler(event(**FULL))
    clock.now += 5
    broker.handler(event(p="10.0000", p1="10.0000"))
    assert feed.latest["p"] == 10.0
    assert feed.latest["iE"] == 1000.1


def test_unreadable_values_are_skipped_not_fatal():
    feed, broker, _ = started()
    broker.handler(event(p="12.0000", p1="n/a", p2=None, p3={"x": 1}))
    assert feed.latest == {"p": 12.0}


def test_junk_payloads_do_not_raise():
    feed, broker, _ = started()
    for junk in ("", "nope", "[]", "{}", b"\xff", json.dumps({"identifier": 5})):
        broker.handler(junk)
    assert feed.latest is None


def test_listeners_are_throttled_but_the_value_is_not():
    feed, broker, clock = started(min_interval=1.0)
    calls = []
    feed.add_listener(lambda: calls.append(feed.latest["p"]))
    broker.handler(event(p="1.0000"))
    clock.now += 0.52
    broker.handler(event(p="2.0000"))          # inside the interval
    assert calls == [1.0]
    assert feed.latest["p"] == 2.0             # but never stale
    clock.now += 0.6
    broker.handler(event(p="3.0000"))
    assert calls == [1.0, 3.0]


def test_a_removed_listener_is_not_called_and_removing_twice_is_fine():
    feed, broker, _ = started()
    calls = []
    remove = feed.add_listener(lambda: calls.append(1))
    remove()
    remove()
    broker.handler(event(p="1.0000"))
    assert calls == []


def test_a_failing_listener_does_not_starve_the_others():
    feed, broker, _ = started()
    calls = []

    def bad():
        raise RuntimeError("boom")

    feed.add_listener(bad)
    feed.add_listener(lambda: calls.append(1))
    broker.handler(event(p="1.0000"))
    assert calls == [1]


def test_start_twice_subscribes_once_and_stop_unsubscribes_once():
    async def scenario():
        clock, broker = Clock(), Broker()
        feed = SemFeed(SEM, broker.subscribe, clock=clock)
        await feed.async_start()
        first = broker.handler
        await feed.async_start()
        assert broker.handler is first
        await feed.async_stop()
        await feed.async_stop()
        assert broker.unsubscribed == 1

    asyncio.run(scenario())


def test_stop_survives_an_unsubscribe_that_raises():
    async def scenario():
        async def subscribe(topic, handler):
            def unsubscribe():
                raise RuntimeError("gone")
            return unsubscribe

        feed = SemFeed(SEM, subscribe)
        await feed.async_start()
        await feed.async_stop()

    asyncio.run(scenario())


def test_the_last_event_of_a_burst_is_announced_after_the_interval():
    """The meter can go quiet for 13-16 s right after a burst. A throttled
    last event must not wait for the next one to be seen."""
    async def scenario():
        broker = Broker()
        feed = SemFeed(SEM, broker.subscribe, min_interval=0.05)   # real clock
        seen = []
        feed.add_listener(lambda: seen.append(feed.latest["p"]))
        await feed.async_start()
        broker.handler(event(p="1.0000"))
        broker.handler(event(p="2.0000"))          # throttled
        broker.handler(event(p="3.0000"))          # throttled, same trailing call
        assert seen == [1.0]
        await asyncio.sleep(0.15)
        assert seen == [1.0, 3.0]
        await feed.async_stop()

    asyncio.run(scenario())


def test_stopping_cancels_the_trailing_announcement():
    async def scenario():
        broker = Broker()
        feed = SemFeed(SEM, broker.subscribe, min_interval=0.05)
        seen = []
        feed.add_listener(lambda: seen.append(1))
        await feed.async_start()
        broker.handler(event(p="1.0000"))
        broker.handler(event(p="2.0000"))
        await feed.async_stop()
        await asyncio.sleep(0.15)
        assert seen == [1]

    asyncio.run(scenario())


def test_non_finite_values_never_reach_the_sensors():
    """Anyone can publish to the topic, and "nan"/"inf" parse as floats."""
    feed, broker, _ = started()
    broker.handler(event(p="10.0000", p1="nan", p2="inf", p3="-inf", iE="NaN"))
    assert feed.latest == {"p": 10.0}
    broker.handler(event(p="nan"))                  # no usable total: no reading at all
    assert feed.latest == {"p": 10.0}

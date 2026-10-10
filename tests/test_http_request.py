"""The local HTTP request helper: what it raises, what it logs, what it counts.

A missed answer used to log "Error requesting data from <url>: " at error level
-- with nothing after the colon, a TimeoutError has no text -- on every miss,
also when the coordinator was riding it out with the last values. Now the
helper raises with a message that names the endpoint and the wait, logs at
debug only, and counts the requests that are on the wire.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

aiohttp = pytest.importorskip("aiohttp")

from ezhi_component.api import APsystemsEZHI  # noqa: E402


class Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    async def json(self):
        return self._body


class Session:
    """A session whose answers are scripted: a body, an exception, or a delay."""

    def __init__(self, *script):
        self.script = list(script)
        self.urls: list[str] = []

    async def get(self, url, params=None):
        self.urls.append(url)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, (int, float)):
            await asyncio.sleep(item)
            return Response({"message": "SUCCESS"})
        return Response(item)


def api_with(session, timeout=0.05):
    return APsystemsEZHI("192.0.2.1", timeout=timeout, session=session)


def test_a_reply_is_returned_and_logged_at_debug_only(caplog):
    api = api_with(Session({"data": {"p": "1"}}))
    with caplog.at_level(logging.DEBUG, logger="ezhi_component.api"):
        assert asyncio.run(api._request("getPower")) == {"data": {"p": "1"}}
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]
    assert "getPower: answered in" in caplog.text and "1 request(s) in flight" in caplog.text


def test_a_timeout_says_what_did_not_answer_and_is_not_an_error(caplog):
    api = api_with(Session(5))                      # answers after 5 s; the limit is 0.05 s
    with caplog.at_level(logging.DEBUG, logger="ezhi_component.api"):
        with pytest.raises(TimeoutError) as raised:
            asyncio.run(api._request("getOutputData"))
    assert "getOutputData" in str(raised.value) and "0.05 s" in str(raised.value)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "getOutputData: no answer after" in caplog.text


def test_a_connection_error_keeps_its_type_and_is_not_an_error(caplog):
    reset = aiohttp.ClientConnectionError("Connection reset by peer")
    api = api_with(Session(reset))
    with caplog.at_level(logging.DEBUG, logger="ezhi_component.api"):
        with pytest.raises(aiohttp.ClientConnectionError):
            asyncio.run(api._request("getOutputData"))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "Connection reset by peer" in caplog.text


def test_requests_in_flight_are_counted_and_the_count_returns_to_zero(caplog):
    async def go():
        api = api_with(Session(0.05, 0.05), timeout=2)
        await asyncio.gather(api._request("getOutputData"), api._request("getPower"))
        assert api._in_flight == 0

    with caplog.at_level(logging.DEBUG, logger="ezhi_component.api"):
        asyncio.run(go())
    assert "2 request(s) in flight" in caplog.text


def test_the_count_returns_to_zero_after_a_failure():
    async def go():
        api = api_with(Session(5))
        with pytest.raises(TimeoutError):
            await api._request("getAlarm")
        assert api._in_flight == 0

    asyncio.run(go())


@pytest.mark.parametrize("body, expected", [
    ({"data": {"power": "-1200"}}, -1200),
    ({"data": {"power": "-1200.0"}}, -1200),
    ({"data": {"power": -1200}}, -1200),
    ({"data": {"power": "0"}}, 0),
])
def test_get_power_reads_the_setpoint(body, expected):
    assert asyncio.run(api_with(Session(body)).get_power()) == expected


@pytest.mark.parametrize("body", [
    {"data": {}},                       # no field
    {"message": "FAILED"},              # no data at all
    {"data": None},                     # used to crash with an AttributeError
    {"data": {"power": ""}},            # empty
    {"data": {"power": "n/a"}},
    {"data": {"power": None}},
    {"data": {"power": "nan"}},
    {"data": {"power": "inf"}},
    ["not", "an", "object"],
])
def test_get_power_does_not_turn_an_unusable_reply_into_a_setpoint_of_zero(body):
    """0 is a setpoint like any other: a reply without a value is no reading."""
    with pytest.raises(ValueError, match="getPower came without a usable power value"):
        asyncio.run(api_with(Session(body)).get_power())


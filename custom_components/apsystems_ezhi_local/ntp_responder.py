"""Answer the inverter's and the meter's "what time is it" on a local broker.

Home-Assistant-free, like mqtt_api.py: `publish` and `subscribe` are handed in.

Every time the EZHI connects it asks the broker for the time on
/ntp/EZHI/<id>/get, the SEM on /ntp/SEM/<id>/get, and the vendor cloud answers.
A local broker does not -- so without this, or something like it, a redirected
device keeps its clock at the epoch (its events then say deviceTime
19700101000000) and anything keyed to the time of day has nothing to go on.

Optional, and off by default: it changes what the devices see, and an
installation that already has something answering (a script, a second
controller) would otherwise be answered twice. Local Control itself was
measured to start without any answer (2026-10-08), so nothing on that path
needs this.

Only the devices it was told about are answered, by exact topic -- not a
wildcard. A broker shared with other people's devices is none of its business.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Iterable

from . import mqtt_protocol

_LOGGER = logging.getLogger(__name__)


class NtpResponder:
    """Answers the time requests of a fixed set of (product key, device id)."""

    def __init__(
        self,
        publish: Callable[[str, str], Awaitable[Any]],
        subscribe: Callable[[str, Callable[[Any], None]], Awaitable[Callable[[], Any]]],
        targets: Iterable[tuple[str, str]],
        fallback_timezone: Callable[[], str],
        now: Callable[[], Any] | None = None,
    ) -> None:
        self._publish = publish
        self._subscribe = subscribe
        self._targets = tuple(targets)
        self._fallback_timezone = fallback_timezone
        self._now = now
        self._unsubscribe: list[Callable[[], Any]] = []
        self._tasks: set[asyncio.Task] = set()
        self.answered = 0

    async def async_start(self) -> None:
        """Start listening. A second call is a no-op."""
        if self._unsubscribe:
            return
        for product_key, device_id in self._targets:
            topic = mqtt_protocol.topic_ntp_get(device_id, product_key)
            try:
                self._unsubscribe.append(
                    await self._subscribe(topic, self._handler(product_key, device_id)))
            except Exception:
                await self.async_stop()
                raise

    async def async_stop(self) -> None:
        """Stop listening and drop any answer still being sent."""
        while self._unsubscribe:
            try:
                result = self._unsubscribe.pop()()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as err:  # noqa: BLE001 - unloading must not raise
                _LOGGER.debug("unsubscribe failed, continuing: %s", err)
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    def _handler(self, product_key: str, device_id: str) -> Callable[[Any], None]:
        def on_request(payload: Any) -> None:
            try:
                corr_id, requested = mqtt_protocol.parse_ntp_request(payload)
            except ValueError as err:
                # Anyone can publish to a topic. Log it, drop it, stay up.
                _LOGGER.debug("ignoring an unreadable time request: %s", err)
                return
            tz = mqtt_protocol.resolve_timezone(requested, self._fallback_timezone())
            reply = mqtt_protocol.build_ntp_reply(
                device_id, corr_id, tz, product_key,
                None if self._now is None else self._now(),
            )
            task = asyncio.get_running_loop().create_task(
                self._answer(product_key, device_id, reply))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return on_request

    async def _answer(self, product_key: str, device_id: str, reply: str) -> None:
        try:
            await self._publish(mqtt_protocol.topic_ntp_reply(device_id, product_key), reply)
        except Exception as err:  # noqa: BLE001 - one lost answer; the next connect asks again
            _LOGGER.warning("EZHI: could not answer %s's time request: %s", device_id, err)
            return
        self.answered += 1
        _LOGGER.debug("EZHI: answered %s's time request", device_id)

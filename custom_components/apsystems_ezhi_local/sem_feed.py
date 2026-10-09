"""The smart meter's live readings, pushed over the local broker.

Home-Assistant-free: `subscribe` is handed in, the clock is injectable.

Once redirected to the broker, the SEM pushes `outputDataSecond` on
/event/SEM/<id>/post: per-phase power (p1..p3), their sum (p, positive = draw
from the grid), and the cumulative energies -- imported from the grid (iE, with
iE1..iE3 per phase) and exported to it (eE, eE1..eE3). Measured 2026-10-07: the
meter reports on a 0.52 s raster -- every 2.6 s while the load changes, with
gaps of up to 13-16 s when nothing does -- all values as strings with four
decimals. The unit of the energies is kWh: the vendor app labels iE "imported"
and eE "exported" and runs both through its kWh formatter. A capture showed
iE 368.8 and eE 0.0140 -- the export counter is small under Local Control (it
holds the draw above zero) but it does count.

Nothing is polled here: the meter volunteers its values, and asking it adds
nothing the events do not carry.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Awaitable, Callable

from . import mqtt_protocol

_LOGGER = logging.getLogger(__name__)

EVENT_IDENTIFIER = "outputDataSecond"
FIELDS = ("p", "p1", "p2", "p3", "iE", "iE1", "iE2", "iE3", "eE", "eE1", "eE2", "eE3")

# Home Assistant writes a state per change. The meter reports up to twice a
# second; a recorder does not need that, and the dashboards cannot show it.
DEFAULT_MIN_INTERVAL_S = 1.0


class SemFeed:
    """Keeps the latest readings and tells listeners, at most once per interval."""

    def __init__(
        self,
        device_id: str,
        subscribe: Callable[[str, Callable[[Any], None]], Awaitable[Callable[[], Any]]],
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._device_id = device_id
        self._subscribe = subscribe
        self._min_interval = min_interval
        self._clock = clock
        self._unsubscribe: Callable[[], Any] | None = None
        self._listeners: list[Callable[[], None]] = []
        self._latest: dict[str, float] | None = None
        self._stamp: float | None = None
        self._notified: float | None = None
        # The trailing notification of a throttled burst (see _on_event).
        self._pending: asyncio.TimerHandle | None = None

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def latest(self) -> dict[str, float] | None:
        """The most recent readings, or None before the first event."""
        return None if self._latest is None else dict(self._latest)

    @property
    def age(self) -> float | None:
        """Seconds since the last event, or None before the first."""
        return None if self._stamp is None else self._clock() - self._stamp

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` on new readings; returns the function that removes it."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def async_start(self) -> None:
        if self._unsubscribe is not None:
            return
        self._unsubscribe = await self._subscribe(
            mqtt_protocol.topic_event(self._device_id, mqtt_protocol.SEM_PRODUCT_KEY),
            self._on_event,
        )

    async def async_stop(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is None:
            return
        try:
            result = unsubscribe()
            if hasattr(result, "__await__"):
                await result
        except Exception as err:  # noqa: BLE001 - unloading must not raise
            _LOGGER.debug("unsubscribe failed, continuing: %s", err)

    def _on_event(self, payload: Any) -> None:
        try:
            identifier, data = mqtt_protocol.parse_event(payload)
        except ValueError as err:
            _LOGGER.debug("ignoring an unreadable meter event: %s", err)
            return
        if identifier != EVENT_IDENTIFIER:
            return
        values: dict[str, float] = {}
        for key in FIELDS:
            try:
                number = float(data[key])
            except (KeyError, TypeError, ValueError):
                continue
            # Anyone can publish to the topic; "nan" and "inf" parse as floats
            # and would then make every state write of the sensor raise.
            if math.isfinite(number):
                values[key] = number
        if "p" not in values:
            # No total, no reading: a partial event would put a number on
            # the dashboard that nothing vouches for.
            return
        now = self._clock()
        # Keep what was never reported rather than dropping it: the cumulative
        # energies come in the same event as the power, but a trimmed event
        # must not blank them.
        merged = dict(self._latest or {})
        merged.update(values)
        self._latest = merged
        self._stamp = now
        if self._notified is not None and now - self._notified < self._min_interval:
            # Throttled -- but the meter can go quiet for 13-16 s right after
            # a burst, and a swallowed last event would then sit unannounced
            # for that long. So whatever is newest by the time the interval
            # is up gets announced.
            self._schedule_trailing(self._min_interval - (now - self._notified))
            return
        self._notify()

    def _schedule_trailing(self, delay: float) -> None:
        if self._pending is not None:
            return                      # one is already on its way; it reads `latest`
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                      # not on the loop (only in tests): skip it
        self._pending = loop.call_later(max(delay, 0.0), self._flush)

    def _flush(self) -> None:
        self._pending = None
        self._notify()

    def _notify(self) -> None:
        self._notified = self._clock()
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - one bad listener must not stop the rest
                _LOGGER.exception("meter feed listener failed")

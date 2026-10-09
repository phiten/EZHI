"""How long a failing poll may keep showing the last good data.

Home Assistant marks every entity of a coordinator unavailable on the first poll
that fails. The inverter answers late or not at all now and then -- a busy
firmware, a WLAN hiccup, and for a while after a command that makes it
reconnect -- and one missed answer should not blank the whole device.

Home-Assistant-free, with an injectable clock, so the rule can be tested alone.
"""
from __future__ import annotations

import time
from typing import Callable


class Grace:
    """Whether a failed poll may be covered with the data of the last good one.

    A failure is covered while the last success is at most `seconds` ago, or
    while an extension is running (`extend`), whichever lasts longer. Without
    any success so far nothing is covered: there is no good data to show.
    """

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._clock = clock
        self._last_ok: float | None = None
        self._until = 0.0

    def ok(self) -> None:
        """A poll succeeded."""
        self._last_ok = self._clock()

    def extend(self, seconds: float) -> None:
        """Cover failures for `seconds` from now -- after a command that makes
        the device reconnect, when it is expected to be silent for a while."""
        self._until = max(self._until, self._clock() + seconds)

    def holds(self) -> bool:
        """True while a failure may still be covered."""
        if self._last_ok is None:
            return False
        now = self._clock()
        return now < self._until or now - self._last_ok <= self._seconds

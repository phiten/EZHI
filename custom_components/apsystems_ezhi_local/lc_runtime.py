"""The Home Assistant side of Local Control: coordinators, start and stop.

local_control.py, sem_feed.py and ntp_responder.py hold the logic and know
nothing about Home Assistant. This module is the thin layer that gives them a
lifecycle (subscribe on setup, unsubscribe on unload), a polling schedule and
the coordinators the entities hang off.

Unloading the entry deliberately does NOT dissolve the group. The devices
regulate by themselves; Home Assistant is only the configurator and the
monitor, and a reload or a restart must not interrupt the regulation.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .cloud import EzhiCloudError
from .const import (
    CONF_ANSWER_NTP,
    CONF_LOCAL_CONTROL_OFFSET,
    DEFAULT_LOCAL_CONTROL_OFFSET,
    RECONNECT_GRACE_S,
    sem_device_id,
)
from .local_control import (
    LocalControl,
    LocalControlState,
    Problem,
    check_offset,
    problem_of,
    status_of,
    unreadable_problem,
)
from .mqtt_protocol import PRODUCT_KEY, SEM_PRODUCT_KEY
from .sem_feed import SemFeed

_LOGGER = logging.getLogger(__name__)

# How often the group is re-read from the two devices. The devices regulate on
# their own, so this only keeps the switch and the problem sensor honest.
LC_POLL_S = 30
# After a switch or an offset change the group needs about 11 s to reconnect
# and 28 s to regulate; read quickly for a while so the entities follow.
LC_FAST_POLL_S = 5
LC_FAST_FOR_S = 90
# A read that fails once is usually a broker or WLAN hiccup: the last state is
# kept and the Problem sensor stays quiet. The third failure in a row (about a
# minute and a half at the normal pace) is reported.
LC_READ_FAILURES_TOLERATED = 2
# The meter reports at least every ~16 s even when nothing changes; a minute
# without anything means the feed is not arriving.
SEM_STALE_S = 60
SEM_CHECK_S = 15
# Deadline for a switch or number write: two commands, each answered within
# the transport's own reply timeout.
WRITE_TIMEOUT_S = 45


class LocalControlCoordinator(DataUpdateCoordinator):
    """Polls the group state; `data` is a LocalControlState."""

    def __init__(self, hass: HomeAssistant, entry, control: LocalControl,
                 offset: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="APsystems EZHI Local Control",
            update_interval=timedelta(seconds=LC_POLL_S),
            config_entry=entry,
            # LocalControlState is a frozen dataclass: an identical read is
            # equal, and there is nothing to wake the entities for.
            always_update=False,
        )
        self.control = control
        # The offset the next enable uses, in watts. Mirrors the number entity.
        self.offset = offset
        self._fast_until: float | None = None
        # Reads that failed in a row; see LC_READ_FAILURES_TOLERATED.
        self._failures = 0
        # The most recent problem and when it began, kept after it has gone: a
        # fault that cleared by itself would otherwise leave no trace at all.
        self.last_problem: Problem | None = None
        self.last_problem_at = None
        self._seen_code: str | None = None
        # The other coordinators of the entry. Forming or dissolving the group
        # makes the inverter reconnect and go quiet for a while; they keep their
        # last values through it instead of turning every entity unavailable.
        self.peers: list = []

    @property
    def settling(self) -> bool:
        """True for a while after a command, while the group is still coming up."""
        return self._fast_until is not None

    @property
    def problem(self) -> Problem | None:
        """What is wrong with the group right now -- None when it is fine, off or unknown."""
        if not self.last_update_success:
            if self.data is None and self.last_exception is None:
                return None            # nothing has been read yet: no verdict
            return unreadable_problem(self.last_exception)
        if self.data is None:
            return None
        return problem_of(self.data, self.settling)

    @property
    def failures(self) -> int:
        """Reads that failed in a row (0 after a good one)."""
        return self._failures

    @property
    def status(self) -> str | None:
        """The group in one word (local_control.STATUS_OPTIONS); None before the first read."""
        return status_of(self.data, self.problem, self.settling)

    def _restore_pace(self) -> None:
        """Back to the normal poll, unless a command still wants the fast one."""
        if self._fast_until is None:
            self.update_interval = timedelta(seconds=LC_POLL_S)

    def speed_up(self) -> None:
        """Read often for a while -- after an enable, a disable, an offset change."""
        self._fast_until = time.monotonic() + LC_FAST_FOR_S
        self.update_interval = timedelta(seconds=LC_FAST_POLL_S)
        for peer in self.peers:
            extend = getattr(peer, "extend_grace", None)
            if extend is not None:
                extend(RECONNECT_GRACE_S)

    def _note(self, problem: Problem | None) -> None:
        """Log a problem once when it appears and once when it is gone.

        The Problem sensor shows the state at the moment someone looks; the log
        keeps what it was when nobody did.
        """
        code = None if problem is None else problem.code
        if code == self._seen_code:
            return
        previous, self._seen_code = self._seen_code, code
        if problem is not None:
            self.last_problem = problem
            self.last_problem_at = dt_util.utcnow()
            _LOGGER.warning("Local Control problem: %s", problem.text)
        elif previous is not None:
            _LOGGER.info("Local Control: the problem (%s) is gone", previous)

    async def _async_update_data(self) -> LocalControlState | None:
        if self._fast_until is not None and time.monotonic() >= self._fast_until:
            self._fast_until = None
            self.update_interval = timedelta(seconds=LC_POLL_S)
            # What the Problem sensor says depends on `settling`, and an
            # unchanged read does not wake the entities (always_update=False).
            self.async_update_listeners()
        try:
            state = await self.control.async_read_state()
        except EzhiCloudError as err:
            self._failures += 1
            tolerated = self._failures <= LC_READ_FAILURES_TOLERATED
            if self.data is not None and (self._fast_until is not None or tolerated):
                # Right after a change the inverter drops off the broker for
                # about 11 s while it reconnects, and reads time out in that
                # gap; outside it, a single lost read is a hiccup. Keeping the
                # last state spares the entities a round of "problem".
                _LOGGER.debug(
                    "Local Control: read %d failed, keeping the last state: %s",
                    self._failures, err)
                return self.data
            if self.data is None and tolerated:
                # The first read after a start or a reload often meets a device
                # that is still reconnecting. Nothing is known yet, so nothing
                # is wrong yet: no problem, no error in the log, and another
                # try in a few seconds instead of in half a minute. "No data" is
                # the state before the first read, which every entity handles.
                self.update_interval = timedelta(seconds=LC_FAST_POLL_S)
                _LOGGER.debug(
                    "Local Control: first read %d failed, trying again in %d s: %s",
                    self._failures, LC_FAST_POLL_S, err)
                return None
            self._note(unreadable_problem(err))
            self._restore_pace()
            raise UpdateFailed(f"Local Control: {err}") from err
        self._failures = 0
        self._restore_pace()
        self._note(problem_of(state, self.settling))
        return state


class SemCoordinator(DataUpdateCoordinator):
    """The smart meter's pushed readings as a coordinator.

    `data` is the latest {p, p1.., iE, ..} dict. Nothing is polled: the feed
    pushes, and the periodic refresh below only notices when it has stopped --
    a meter that goes quiet turns its entities unavailable instead of leaving
    the last value on the dashboard looking current.
    """

    def __init__(self, hass: HomeAssistant, entry, feed: SemFeed) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="APsystems SEM",
            update_interval=timedelta(seconds=SEM_CHECK_S),
            config_entry=entry,
            always_update=True,
        )
        self.feed = feed
        self._started = time.monotonic()
        self._remove_listener = feed.add_listener(self._on_feed)
        # Whatever arrived before this coordinator existed.
        self.data = feed.latest

    @callback
    def _on_feed(self) -> None:
        latest = self.feed.latest
        if latest is not None:
            self.async_set_updated_data(latest)

    async def _async_update_data(self) -> dict:
        latest, age = self.feed.latest, self.feed.age
        if latest is None or age is None:
            # Nothing has arrived yet. The meter reports at least every ~16 s,
            # so this is the normal state for the first seconds after a start or
            # a reload -- not an error to log. The entities show "unknown" until
            # the first reading; a meter that stays silent past SEM_STALE_S is
            # reported like one that goes quiet later.
            waited = time.monotonic() - self._started
            if waited > SEM_STALE_S:
                raise UpdateFailed(
                    f"no reading from the smart meter for {waited:.0f} s")
            return {}
        if age > SEM_STALE_S:
            raise UpdateFailed(
                f"no reading from the smart meter for {age:.0f} s")
        return latest

    async def async_shutdown(self) -> None:
        self._remove_listener()
        await super().async_shutdown()


@dataclass
class LocalControlRuntime:
    """What one entry holds for Local Control, so unloading can undo it."""

    control: LocalControl | None = None
    coordinator: LocalControlCoordinator | None = None
    sem_api: Any = None
    sem_feed: SemFeed | None = None
    sem_coordinator: SemCoordinator | None = None
    ntp: Any = None
    # The first read of the group, running in the background after setup.
    first_read: Any = None
    _stoppers: list = field(default_factory=list)

    async def async_stop(self) -> None:
        """Drop every subscription. Never raises: unloading must finish."""
        for stopper in reversed(self._stoppers):
            try:
                await stopper()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("EZHI Local Control: stopping failed: %r", err)
        self._stoppers.clear()


def wants_local_control_runtime(entry_data) -> bool:
    """Whether there is anything to start: a meter, or the time answerer."""
    return bool(sem_device_id(entry_data)) or bool(
        (entry_data or {}).get(CONF_ANSWER_NTP)
    )


async def async_start(hass: HomeAssistant, entry, device_id: str,
                      mqtt_api) -> LocalControlRuntime:
    """Bring up whatever the entry configures, or nothing at all.

    Called with the inverter's transport already subscribed and the broker
    known to be ready. Everything it started is stopped again before an error
    leaves, so a failure here never leaves subscriptions behind.
    """
    # Imported here for the same reason as make_mqtt_api: the mqtt integration
    # is a soft dependency.
    from .mqtt_connect import make_ntp_responder, make_sem_api, make_sem_feed

    sem_id = sem_device_id(entry.data)
    runtime = LocalControlRuntime()
    try:
        if sem_id:
            sem_api = make_sem_api(hass, sem_id)
            await sem_api.async_subscribe()
            runtime._stoppers.append(sem_api.async_unsubscribe)
            runtime.sem_api = sem_api

            feed = make_sem_feed(hass, sem_id)
            await feed.async_start()
            runtime._stoppers.append(feed.async_stop)
            runtime.sem_feed = feed
            runtime.sem_coordinator = SemCoordinator(hass, entry, feed)

            runtime.control = LocalControl(mqtt_api, sem_api, device_id, sem_id)
            runtime.coordinator = LocalControlCoordinator(
                hass, entry, runtime.control, _stored_offset(entry.data))
            # The first read of the group does not hold up the setup: it goes to
            # two devices and either may be slow or silent, and the local
            # sensors must not wait for that. The entities start with no state
            # and fill in when it arrives (normally within a second or two).
            # async_refresh, not the first-refresh variant: that one raises
            # ConfigEntryNotReady and would take the whole entry down over a
            # meter that is merely not answering yet.
            runtime.first_read = entry.async_create_background_task(
                hass,
                runtime.coordinator.async_refresh(),
                name="apsystems_ezhi_local first Local Control read",
            )

        if entry.data.get(CONF_ANSWER_NTP):
            targets = [(PRODUCT_KEY, device_id)]
            if sem_id:
                targets.append((SEM_PRODUCT_KEY, sem_id))
            ntp = make_ntp_responder(hass, targets, lambda: hass.config.time_zone)
            await ntp.async_start()
            runtime._stoppers.append(ntp.async_stop)
            runtime.ntp = ntp
            _LOGGER.info(
                "EZHI: answering the time requests of %s on the local broker",
                ", ".join(device for _key, device in targets))
    except BaseException:
        await runtime.async_stop()
        raise
    return runtime


def _stored_offset(entry_data) -> int:
    """The saved offset, or the default when it is missing or no longer valid."""
    raw = (entry_data or {}).get(CONF_LOCAL_CONTROL_OFFSET, DEFAULT_LOCAL_CONTROL_OFFSET)
    try:
        return check_offset(raw)
    except Exception:  # noqa: BLE001 - a bad stored value must not stop the entry
        return DEFAULT_LOCAL_CONTROL_OFFSET

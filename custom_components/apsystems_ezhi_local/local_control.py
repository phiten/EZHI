"""Local Control: the inverter regulates to zero feed-in by itself, off a meter group.

Home-Assistant-free on purpose, like mqtt_protocol.py and mqtt_api.py: the tests
drive it with fake devices, and nothing in here imports homeassistant.

What it is, in the app's words "direct power control on site, for devices on the
same router": a smart meter (SEM3-WL-2) and the inverter are put into a *group*
by two commands, and from then on the inverter reads the meter itself and holds
the grid draw at an offset. Home Assistant is the configurator and the monitor,
not the controller -- when it is down, the group keeps regulating (an MQTT
disconnect was measured to leave it running).

The two commands, verified against real hardware over a local broker
(2026-10-07/08):

    meter     localLink  {"status": "1", "config": CFG}
    inverter  systemMode {"systemMode": "1", "thirdLink": "4", "config": CFG}

with the same CFG on both:

    {"meter": "<SEM id>", "power": "30", "vrn": "<6 digits>",
     "totalPower": "1200", "totalPvPower": "1200",
     "device": {"<inverter id>": "1.00"}}

All values are strings. The meter goes first. The group dissolves in the other
order. Note what CFG does NOT contain: no IP address. The inverter finds the
meter itself -- it asks the network (mDNS) for the meter's service by id and
then polls it on TCP port 3333 about every two seconds -- which is why the two
devices have to be on the same network segment, and why nothing here needs, or
accepts, an address.

Not to be confused with the inverter's "Local" *mode* (systemMode "4"), the one
in which the HTTP `setPower` is obeyed. Local Control runs in Balcony Storage
(systemMode "1"); the "4" in `thirdLink` has nothing to do with mode 4.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

from .cloud import EzhiCloudError, wire_str

_LOGGER = logging.getLogger(__name__)

# The D02's nominal output, from the app's own table. The app's group page
# (`devicePowerInfo`) sums a table of nominal powers by device type (D02 1200 W,
# F04 2000 W, E01 500 W, ...) into `totalPower`, and the D02 entries into
# `totalPvPower`; it never reads the inverter's `powerLimit` (800/1200 W). So
# 1200 is what the app sends for an EZHI set to 800 W as well, and it was
# measured to regulate correctly there (2026-10-08: about 340 W output, grid
# draw 33 W on an offset of 30 W). Deliberately NOT derived from `powerLimit`.
# The app caps the offset at 10 % of the group's total power.
TOTAL_POWER_W = 1200
OFFSET_MAX_FRACTION = 0.10
DEFAULT_OFFSET_W = 30

THIRD_LINK_GROUP = "4"

# How long the last command outranks what the poll says about the inverter.
INTENT_FOR_S = 90.0

# meterStatus.isTcpNoDataCount counts seconds without a reading from the meter.
# While regulating it sat at 0-2 (19 of 19 reads); after a reconnect it climbs
# from 15 and more. Ten is a margin of several missed polls, not a derived value.
NO_DATA_LIMIT = 10

# After the inverter accepts the group it reconnects (~11 s) and starts
# regulating (~28 s). 90 s leaves room for a slow broker without waiting forever.
LINK_WAIT_S = 90.0
LINK_POLL_S = 5.0


class LocalControlError(EzhiCloudError):
    """Local Control could not be set up, checked or dissolved.

    An EzhiCloudError on purpose: switch.py, number.py and select.py catch that
    family and nothing else, so anything outside it would reach the user as an
    unhandled exception.
    """


# --- the group's configuration --------------------------------------------------

def max_offset(total_power: float = TOTAL_POWER_W) -> int:
    """The largest offset the app accepts: 10 % of the group's total power."""
    return int(OFFSET_MAX_FRACTION * total_power)


def check_offset(offset: Any, total_power: float = TOTAL_POWER_W) -> int:
    """The offset in whole watts, or LocalControlError if the app would refuse it.

    The offset is how much grid draw the inverter leaves standing, so a larger
    one is the safer side of the same error. Negative is refused: "regulate to
    feed in" is a different function (the app's "Power Limit") that the
    firmware's local mask does not know and that was never tried here.
    """
    try:
        value = float(offset)
    except (TypeError, ValueError):
        raise LocalControlError(f"the offset {offset!r} is not a number") from None
    if not math.isfinite(value):
        raise LocalControlError(f"the offset {offset!r} is not a number")
    limit = max_offset(total_power)
    if value < 0 or value > limit:
        raise LocalControlError(
            f"the offset {value:g} W is outside 0 .. {limit} W "
            f"(10 % of {total_power:g} W, the limit of the vendor app)"
        )
    return int(round(value))


def new_vrn(rng: Any = random) -> str:
    """A fresh six-digit group version, as the app draws one."""
    return str(rng.randint(100000, 999999))


def group_config(
    ezhi_id: str,
    sem_id: str,
    offset: Any,
    vrn: str,
    total_power: float = TOTAL_POWER_W,
    share: str = "1.00",
) -> dict:
    """The `config` both devices get -- all values strings, as sent by the app."""
    return {
        "meter": sem_id,
        "power": str(check_offset(offset, total_power)),
        "vrn": str(vrn),
        "totalPower": str(int(total_power)),
        "totalPvPower": str(int(total_power)),
        "device": {ezhi_id: share},
    }


def as_config(value: Any) -> dict:
    """A device's `config` as a dict.

    Both devices were seen to return an object; the app's code (and so possibly
    other firmware) uses JSON text. Anything else is "no config".
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


# --- reading the state ----------------------------------------------------------

@dataclass(frozen=True)
class LocalControlState:
    """What the two devices say about the group, put side by side."""

    ezhi_member: bool           # inverter: thirdLink "4"
    sem_member: bool            # meter: localLink status "1"
    consistent: bool            # both configs name this meter and this inverter, same vrn
    offset: int | None          # the inverter's config.power
    vrn: str | None
    no_data_count: int | None   # meterStatus.isTcpNoDataCount
    meter_power: float | None   # meterStatus.meterPower: what the inverter sees
    # The raw values behind ezhi_member / sem_member, and the reasons the two
    # configurations differ -- what the Problem sensor shows instead of a bare
    # "something is wrong". Defaults keep a state built without them valid.
    third_link: str | None = None       # inverter systemMode.thirdLink; "4" is a group
    sem_status: str | None = None       # meter localLink.status; "1" is a member
    mismatch: tuple[str, ...] = ()      # what differs between the two configs

    @property
    def active(self) -> bool:
        """Both halves set, and they describe the same group."""
        return self.ezhi_member and self.sem_member and self.consistent

    @property
    def partial(self) -> bool:
        """One half set, or two halves that do not match -- nothing regulates."""
        return (self.ezhi_member or self.sem_member) and not self.active

    @property
    def data_flowing(self) -> bool | None:
        """Does the inverter get readings from the meter? None when unknown."""
        if self.no_data_count is None:
            return None
        return self.no_data_count <= NO_DATA_LIMIT

    @property
    def problem(self) -> bool:
        """A group that was asked for but is not working.

        False when no group exists: off is not a fault.
        """
        if self.partial:
            return True
        return self.active and self.data_flowing is False


@dataclass(frozen=True)
class Problem:
    """Why a group that was asked for is not working.

    `code` is for automations (one of the PROBLEM_* constants); `summary` says
    what is wrong, `facts` are the values it was judged by, `hint` is what to do
    about it. `text` is the three as one paragraph.
    """

    code: str
    summary: str
    facts: tuple[str, ...] = ()
    hint: str = ""

    @property
    def text(self) -> str:
        text = self.summary
        if self.facts:
            text += " (" + "; ".join(self.facts) + ")"
        text += "."
        return f"{text} {self.hint}" if self.hint else text


PROBLEM_INVERTER_ONLY = "inverter_only"
PROBLEM_METER_ONLY = "meter_only"
PROBLEM_MISMATCH = "mismatch"
PROBLEM_NO_DATA = "no_data"
PROBLEM_UNREADABLE = "unreadable"

_RESET_HINT = "Nothing regulates. Switch Local Control off and on again."


def _member_facts(state: LocalControlState) -> tuple[str, ...]:
    """The two raw values that decide membership, as the devices report them."""
    return (
        f"inverter thirdLink {state.third_link if state.third_link is not None else '?'}"
        f" (a Local Control group is {THIRD_LINK_GROUP})",
        f"meter localLink status {state.sem_status if state.sem_status is not None else '?'}"
        " (a member is 1)",
    )


def unreadable_problem(error: Any) -> Problem:
    """The devices could not be read at all -- the verdict for a failed poll."""
    return Problem(
        PROBLEM_UNREADABLE,
        "the inverter or the smart meter did not answer",
        (str(error),) if error is not None and str(error) else (),
        "Check that both are powered and connected to the broker. A group that "
        "stands keeps regulating without Home Assistant.",
    )


def problem_of(state: LocalControlState, settling: bool = False) -> Problem | None:
    """Why a group is not working -- None when it is fine or off.

    `settling` is true for a while after a command. The inverter reconnects
    then (about 11 s) and its counter of seconds without meter data climbs past
    the limit before regulation starts (about 28 s), so "no readings yet" is the
    normal state there and not worth a notification. A half-set or mismatched
    group is a problem either way.
    """
    if state.partial:
        if state.ezhi_member and state.sem_member:
            return Problem(
                PROBLEM_MISMATCH,
                "inverter and meter are both in a group, but not in the same one",
                state.mismatch or _member_facts(state),
                _RESET_HINT,
            )
        if state.ezhi_member:
            return Problem(
                PROBLEM_INVERTER_ONLY,
                "only the inverter is in the group, the smart meter is not",
                _member_facts(state),
                _RESET_HINT,
            )
        return Problem(
            PROBLEM_METER_ONLY,
            "only the smart meter is in the group, the inverter is not",
            _member_facts(state),
            _RESET_HINT + " If this followed a change of the inverter's system "
            "mode, Backup Power, ECO or an SOC limit, that is the lead: whether "
            "such a write drops the inverter out of the group is not established.",
        )
    if state.active and state.data_flowing is False and not settling:
        return Problem(
            PROBLEM_NO_DATA,
            "the group stands on both devices, but the inverter gets no "
            f"readings from the meter (for {state.no_data_count} s)",
            (),
            "Check that both are on the same network segment, with mDNS and "
            "TCP port 3333 allowed between them, and that the meter is powered.",
        )
    return None


def describe_problem(state: LocalControlState, settling: bool = False) -> str | None:
    """`problem_of` as a sentence -- None when the group is fine or off."""
    problem = problem_of(state, settling)
    return None if problem is None else problem.text


def _same(a: Any, b: Any) -> bool:
    """Two config values equal as the devices mean them: 5 and "5" are one value."""
    return a is not None and b is not None and str(a).strip() == str(b).strip()


def config_differences(
    ezhi_cfg: dict, sem_cfg: dict, ezhi_id: str, sem_id: str
) -> tuple[str, ...]:
    """What keeps two group configurations from being one group; empty when they are.

    Each entry names the field and both values, so a mismatch can be read off
    the Problem sensor instead of being reproduced with a packet capture.
    """
    out: list[str] = []
    if not ezhi_cfg:
        out.append("the inverter holds no group configuration")
    else:
        if not _same(ezhi_cfg.get("meter"), sem_id):
            out.append(
                f"the inverter's group names the meter {ezhi_cfg.get('meter')!r}, "
                f"not {sem_id!r}")
        devices = ezhi_cfg.get("device")
        if not isinstance(devices, dict) or ezhi_id not in devices:
            listed = sorted(devices) if isinstance(devices, dict) else devices
            out.append(
                f"the inverter's group does not list the inverter {ezhi_id!r} "
                f"(it lists {listed!r})")
    if not sem_cfg:
        out.append("the meter holds no group configuration")
    elif not _same(sem_cfg.get("meter"), sem_id):
        out.append(
            f"the meter's group names the meter {sem_cfg.get('meter')!r}, "
            f"not {sem_id!r}")
    if ezhi_cfg and sem_cfg and not _same(ezhi_cfg.get("vrn"), sem_cfg.get("vrn")):
        out.append(
            f"the group version differs: inverter {ezhi_cfg.get('vrn')!r}, "
            f"meter {sem_cfg.get('vrn')!r}")
    return tuple(out)


def evaluate(
    ezhi_config: dict,
    sem_link: dict,
    meter_status: dict | None,
    ezhi_id: str,
    sem_id: str,
) -> LocalControlState:
    """Fold the three reads into one state. Pure; the tests run it on real payloads."""
    third_link = ezhi_config.get("thirdLink")
    sem_status = sem_link.get("status")
    ezhi_member = wire_str(third_link if third_link is not None else "0") == THIRD_LINK_GROUP
    sem_member = wire_str(sem_status if sem_status is not None else "0") == "1"

    ezhi_cfg = as_config(ezhi_config.get("config"))
    sem_cfg = as_config(sem_link.get("config"))
    differences = config_differences(ezhi_cfg, sem_cfg, ezhi_id, sem_id)

    meter = meter_status or {}
    vrn = ezhi_cfg.get("vrn")
    return LocalControlState(
        ezhi_member=ezhi_member,
        sem_member=sem_member,
        consistent=not differences,
        offset=_to_int(ezhi_cfg.get("power")),
        vrn=None if vrn is None else str(vrn),
        no_data_count=_to_int(meter.get("isTcpNoDataCount")),
        meter_power=_to_float(meter.get("meterPower")),
        third_link=None if third_link is None else wire_str(third_link),
        sem_status=None if sem_status is None else wire_str(sem_status),
        mismatch=differences,
    )


def _raise_anything_unexpected(results: list) -> None:
    """Re-raise every failure that is not a known transport error (see mqtt_api)."""
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, EzhiCloudError):
            raise result


# --- the controller -------------------------------------------------------------

class LocalControl:
    """Sets up, checks and dissolves one inverter-and-meter group.

    `ezhi` is an EzhiMqttApi, `sem` a SemMqttApi (or anything with the same
    methods). `sleep` and `monotonic` are injectable so the tests need no real
    waiting.
    """

    def __init__(
        self,
        ezhi: Any,
        sem: Any,
        ezhi_id: str,
        sem_id: str,
        *,
        total_power: float = TOTAL_POWER_W,
        rng: Any = random,
        sleep: Callable[[float], Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ezhi = ezhi
        self._sem = sem
        self._ezhi_id = ezhi_id
        self._sem_id = sem_id
        self._total_power = total_power
        self._rng = rng
        self._sleep = sleep
        self._monotonic = monotonic
        # Setting and dissolving must not interleave: two quick taps on a
        # switch would otherwise send one command pair into the other.
        self._lock = asyncio.Lock()
        # The last command given, and when: see inverter_may_be_grouped.
        self._intent: bool | None = None
        self._intent_at = 0.0

    async def _read_raw(self) -> tuple[dict, dict, dict | None]:
        """The three reads: (inverter systemMode, meter localLink, meterStatus).

        The inverter's `systemMode` and the meter's `localLink` are required --
        without them there is no state to report. `meterStatus` is a bonus: a
        failed read leaves the data-flow fields unknown instead of failing.
        """
        config, link, meter = await asyncio.gather(
            self._ezhi.async_get_config(),
            self._sem.async_get_local_link(),
            self._ezhi.async_get_raw("meterStatus"),
            return_exceptions=True,
        )
        _raise_anything_unexpected([config, link, meter])
        failed = [r for r in (config, link) if isinstance(r, BaseException)]
        if len(failed) == 2:
            # Both silent: say so, instead of blaming only whichever came first.
            raise LocalControlError(f"{failed[0]}; {failed[1]}") from failed[0]
        if failed:
            raise failed[0]
        if isinstance(meter, BaseException):
            meter = None
        return config, link, meter

    async def async_read_state(self) -> LocalControlState:
        """Read the group from both devices."""
        config, link, meter = await self._read_raw()
        return evaluate(config, link, meter, self._ezhi_id, self._sem_id)

    def inverter_may_be_grouped(self, state: LocalControlState | None) -> bool:
        """Whether the inverter is, or has just been told to be, in the group.

        The last command wins for a while, because the polled state lags it by
        up to a read cycle: a write guard going by the poll alone would let a
        System Mode change through in the seconds right after the switch went on.
        """
        if self._intent is not None and self._monotonic() - self._intent_at < INTENT_FOR_S:
            return self._intent
        return bool(state is not None and state.ezhi_member)

    def _remember(self, grouped: bool) -> None:
        self._intent = grouped
        self._intent_at = self._monotonic()

    async def async_enable(
        self,
        offset: Any = DEFAULT_OFFSET_W,
        *,
        wait: bool = False,
        link_wait: float = LINK_WAIT_S,
        force: bool = False,
    ) -> LocalControlState | None:
        """Put the meter and the inverter into one group.

        Returns once both devices accepted their command; with `wait` it then
        polls until the inverter reports readings from the meter, which takes
        about 30 s, and returns that state (or raises if it never does).

        A group that already stands with this very offset is left alone, unless
        `force`: forming it again draws a new version, the inverter reconnects
        and does not regulate for about 40 s -- too high a price for an
        automation that re-asserts "on" at every start or every night.
        """
        offset = check_offset(offset, self._total_power)
        async with self._lock:
            before, previous = await self._look()
            if (not force and before is not None and before.active
                    and before.offset == offset):
                _LOGGER.debug("Local Control: already active with %s W, nothing to send", offset)
                self._remember(True)
            else:
                config = group_config(
                    self._ezhi_id, self._sem_id, offset, new_vrn(self._rng),
                    self._total_power,
                )
                await self._join(config, previous)
                self._remember(True)
        if not wait:
            return None
        return await self.async_wait_until_working(link_wait)

    async def _look(self) -> tuple[LocalControlState | None, dict]:
        """What stands now, before a change: (state, the meter's group config).

        Both are "nothing" when the devices do not answer; the writes that
        follow will then say so themselves.
        """
        try:
            config, link, meter = await self._read_raw()
        except EzhiCloudError:
            return None, {}
        state = evaluate(config, link, meter, self._ezhi_id, self._sem_id)
        previous = as_config(link.get("config")) if state.sem_member else {}
        return state, previous

    async def _join(self, config: dict, previous: dict) -> None:
        # The meter first. On its own it changes nothing at the inverter
        # (measured), so a failure of the second command can be undone at the
        # meter alone -- see _settle.
        await self._sem.async_set_local_link(True, config)
        try:
            await self._ezhi.async_set_local_group(config)
        except (Exception, asyncio.CancelledError):
            # CancelledError too: the caller's deadline arrives that way, and
            # the meter has already taken its half by then.
            await self._settle(config, previous)
            raise

    async def _settle(self, new: dict, previous: dict) -> None:
        """After the inverter's command failed or went unanswered: back to a pair
        that agrees, judged by looking and not by guessing.

        A failed or timed-out write is ambiguous -- the inverter may have taken
        it and lost its answer -- so the inverter is asked what it holds, and
        the meter is made to match *that*.
        """
        try:
            inverter = await self._ezhi.async_get_config()
        except (Exception, asyncio.CancelledError):
            inverter = None
        grouped = inverter is not None and (
            wire_str(inverter.get("thirdLink", "0")) == THIRD_LINK_GROUP)
        if grouped and as_config(inverter.get("config")).get("vrn") == new.get("vrn"):
            _LOGGER.warning(
                "Local Control: the inverter took the group although its answer "
                "was lost; both halves agree")
            return
        try:
            if previous:
                # A group stood before this attempt (an offset change): put the
                # meter back as it was, so that it still matches the inverter.
                await self._sem.async_set_local_link(True, previous)
            else:
                if grouped:
                    # Joined with something else than we sent -- not expected;
                    # leave it the verified way, the inverter first.
                    await self._ezhi.async_clear_local_group()
                await self._sem.async_set_local_link(False)
        except (Exception, asyncio.CancelledError) as err:
            _LOGGER.warning(
                "Local Control: putting the meter back after a failed attempt "
                "failed too: %r", err)

    async def async_wait_until_working(self, link_wait: float = LINK_WAIT_S) -> LocalControlState:
        """Poll until both halves match and the inverter gets readings."""
        deadline = self._monotonic() + link_wait
        last: LocalControlState | None = None
        while True:
            try:
                last = await self.async_read_state()
            except EzhiCloudError:
                # The inverter reconnects right after accepting the group, and
                # reads in that gap time out. That is expected, not a failure.
                last = None
            if last is not None and last.active and last.data_flowing:
                return last
            if self._monotonic() >= deadline:
                break
            await self._sleep(LINK_POLL_S)
        if last is not None and last.active and last.data_flowing is None:
            raise LocalControlError(
                "the group is set on both devices, but the inverter's "
                "meterStatus could not be read, so it is not known whether "
                "readings arrive. Check the Local Control Problem sensor "
                "and the meter's readings"
            )
        raise LocalControlError(
            "the group was set, but the inverter reports no readings from the "
            f"meter after {link_wait:.0f} s. The usual cause is the network: the "
            "inverter and the meter must be on the same segment, with mDNS and "
            "TCP port 3333 allowed between them"
            + ("" if last is None else
               f" (state: inverter {'in' if last.ezhi_member else 'not in'} the group, "
               f"meter {'in' if last.sem_member else 'not in'} it, "
               f"seconds without data: "
               f"{'unknown' if last.no_data_count is None else last.no_data_count})")
        )

    async def async_disable(self) -> None:
        """Dissolve the group: the inverter first, then the meter.

        The inverter's output stops with its half, so a failure of the meter's
        half afterwards leaves a harmless leftover -- and is reported, because
        a meter still marked as a member would show up as a half-set group.
        """
        async with self._lock:
            await self._ezhi.async_clear_local_group()
            self._remember(False)
            await self._sem.async_set_local_link(False)

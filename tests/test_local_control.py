"""Local Control: the group of one inverter and one meter.

The payload fixtures are what the real devices answered on a local broker on
2026-10-08 with the group running (serials scrubbed): the inverter's systemMode
with thirdLink "4", the meter's localLink with status "1", and meterStatus with
the inverter reading the meter. The "off" payloads are the same devices without
a group.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from ezhi_component import local_control as lc
from ezhi_component.cloud import EzhiCloudError
from ezhi_component.local_control import LocalControl, LocalControlError

EZHI = "D00000000000"
SEM = "M00000000000"

CFG = {
    "device": {EZHI: "1.00"}, "meter": SEM, "power": "30",
    "totalPower": "1200", "totalPvPower": "1200", "vrn": "184486",
}

EZHI_ON = {
    "ECO": "0", "EPS": "0", "acSwitch": "0", "config": CFG,
    "dischargeProtection": "12", "grid": "0", "isOPStrategy": "1",
    "noBattery": "0", "onOff": "0", "outputPowerStrategy": [],
    "outputPowerStrategyWeekly": [], "powerLimit": "1200", "singlePhase": "0",
    "socMax": "100", "socMin": "10", "stayOn": "0", "systemMode": "1",
    "thirdLink": "4", "userSetPower": "0", "winter": "1",
}
EZHI_OFF = {**EZHI_ON, "thirdLink": "0", "config": {}}
SEM_ON = {"config": CFG, "status": "1"}
SEM_OFF = {"status": "0"}                     # the unit answers just this when ungrouped
METER_OK = {"channel": 1, "channel0": 0, "company": "0", "isTcpNoDataCount": 2,
            "meterPower": "30", "rssi": 0, "status": "1"}
METER_IDLE = {**METER_OK, "isTcpNoDataCount": 5, "meterPower": "0"}


# --- the offset ------------------------------------------------------------------

def test_the_offset_may_be_anything_the_app_allows():
    assert lc.max_offset() == 120
    assert lc.check_offset(0) == 0
    assert lc.check_offset(30) == 30
    assert lc.check_offset(120) == 120
    assert lc.check_offset("45") == 45
    assert lc.check_offset(29.6) == 30


@pytest.mark.parametrize("bad", [-1, -0.5, 120.5, 1000, "abc", None, float("nan"), float("inf")])
def test_an_offset_the_app_would_refuse_is_refused(bad):
    with pytest.raises(LocalControlError):
        lc.check_offset(bad)


def test_the_offset_limit_follows_the_total_power():
    assert lc.max_offset(800) == 80
    with pytest.raises(LocalControlError):
        lc.check_offset(100, total_power=800)


# --- the config -------------------------------------------------------------------

def test_the_config_is_what_the_devices_hold():
    assert lc.group_config(EZHI, SEM, 30, "184486") == CFG


def test_every_config_value_is_a_string_and_there_is_no_address():
    cfg = lc.group_config(EZHI, SEM, 30.0, 184486)
    for key in ("meter", "power", "vrn", "totalPower", "totalPvPower"):
        assert isinstance(cfg[key], str), key
    assert cfg["device"] == {EZHI: "1.00"}
    assert set(cfg) == {"meter", "power", "vrn", "totalPower", "totalPvPower", "device"}
    assert "ip" not in json.dumps(cfg).lower().replace("pvpower", "")


def test_vrn_is_six_digits():
    for _ in range(50):
        vrn = lc.new_vrn()
        assert len(vrn) == 6 and vrn.isdigit()


def test_a_config_may_arrive_as_json_text():
    assert lc.as_config(json.dumps(CFG)) == CFG
    assert lc.as_config(CFG) == CFG
    for junk in (None, "", "   ", "{not json", "[1]", 7):
        assert lc.as_config(junk) == {}


# --- reading the state ------------------------------------------------------------

def test_a_working_group_reads_as_active():
    st = lc.evaluate(EZHI_ON, SEM_ON, METER_OK, EZHI, SEM)
    assert st.active and not st.partial and not st.problem
    assert st.ezhi_member and st.sem_member and st.consistent
    assert st.offset == 30
    assert st.vrn == "184486"
    assert st.no_data_count == 2
    assert st.meter_power == 30.0
    assert st.data_flowing is True


def test_no_group_is_off_and_not_a_problem():
    st = lc.evaluate(EZHI_OFF, SEM_OFF, METER_IDLE, EZHI, SEM)
    assert not st.active and not st.partial and not st.problem
    assert st.offset is None
    assert st.data_flowing is True      # 5 s is under the limit; with no group the field means nothing anyway


def test_one_half_is_a_problem():
    only_meter = lc.evaluate(EZHI_OFF, SEM_ON, METER_IDLE, EZHI, SEM)
    assert only_meter.partial and only_meter.problem and not only_meter.active
    only_inverter = lc.evaluate(EZHI_ON, SEM_OFF, METER_IDLE, EZHI, SEM)
    assert only_inverter.partial and only_inverter.problem


def test_two_halves_that_do_not_match_are_not_a_group():
    other = {**CFG, "vrn": "999999"}
    st = lc.evaluate(EZHI_ON, {"config": other, "status": "1"}, METER_OK, EZHI, SEM)
    assert st.ezhi_member and st.sem_member
    assert not st.consistent and not st.active and st.partial and st.problem


def test_a_group_naming_another_meter_is_not_this_one():
    st = lc.evaluate(EZHI_ON, SEM_ON, METER_OK, EZHI, "M99999999999")
    assert not st.consistent and not st.active


def test_a_group_without_this_inverter_is_not_this_ones():
    st = lc.evaluate(EZHI_ON, SEM_ON, METER_OK, "D99999999999", SEM)
    assert not st.consistent and not st.active


def test_a_group_whose_meter_is_silent_is_a_problem():
    """Both halves set, but the inverter counts seconds without a reading --
    what a wrong network (another segment, mDNS blocked) looks like."""
    silent = {**METER_OK, "isTcpNoDataCount": 22, "meterPower": "0"}
    st = lc.evaluate(EZHI_ON, SEM_ON, silent, EZHI, SEM)
    assert st.active and st.data_flowing is False and st.problem


def test_the_data_limit_is_inclusive():
    at = {**METER_OK, "isTcpNoDataCount": lc.NO_DATA_LIMIT}
    over = {**METER_OK, "isTcpNoDataCount": lc.NO_DATA_LIMIT + 1}
    assert lc.evaluate(EZHI_ON, SEM_ON, at, EZHI, SEM).data_flowing is True
    assert lc.evaluate(EZHI_ON, SEM_ON, over, EZHI, SEM).data_flowing is False


def test_a_missing_meter_status_leaves_the_data_flow_unknown_not_bad():
    st = lc.evaluate(EZHI_ON, SEM_ON, None, EZHI, SEM)
    assert st.active and st.data_flowing is None and not st.problem
    assert st.no_data_count is None and st.meter_power is None


def test_garbage_in_the_reads_does_not_raise():
    st = lc.evaluate({"thirdLink": "4", "config": "{broken"}, {"status": "1", "config": 5},
                     {"isTcpNoDataCount": "n/a", "meterPower": "x"}, EZHI, SEM)
    assert st.ezhi_member and st.sem_member and not st.consistent
    assert st.no_data_count is None and st.meter_power is None


def test_the_offset_is_read_from_the_inverters_config():
    cfg = {**CFG, "power": "55"}
    st = lc.evaluate({**EZHI_ON, "config": cfg}, {"status": "1", "config": cfg}, METER_OK, EZHI, SEM)
    assert st.offset == 55


# --- the controller ---------------------------------------------------------------

class World:
    """Both devices and the order in which they were spoken to."""

    def __init__(self, *, link_after_reads=0, meter_after_reads=0):
        self.calls: list[str] = []
        self.ezhi = dict(EZHI_OFF)
        self.sem = dict(SEM_OFF)
        self.meter = dict(METER_IDLE)
        self.reads = 0
        self.fail_ezhi_join = None
        self.fail_sem_leave = None
        self.fail_ezhi_leave = None
        self.fail_meter_read = False
        self.fail_ezhi_read = None
        self.fail_sem_read = None
        self.hang_ezhi_join = False
        self.ezhi_join_applies_then_fails = False
        self.reconnecting_reads = 0     # reads that time out, like during the inverter's reconnect
        self.flowing_after = 0          # reads after which the meter delivers

    # inverter side
    async def async_get_config(self):
        self.reads += 1
        if self.fail_ezhi_read:
            raise self.fail_ezhi_read
        if self.reconnecting_reads and self.reads <= self.reconnecting_reads:
            raise EzhiCloudError("timeout")
        return dict(self.ezhi)

    async def async_get_raw(self, identifier):
        assert identifier == "meterStatus"
        if self.fail_meter_read:
            raise EzhiCloudError("no answer")
        if self.ezhi.get("thirdLink") == "4" and self.reads > self.flowing_after:
            return dict(METER_OK)
        return dict(METER_IDLE if self.ezhi.get("thirdLink") != "4"
                    else {**METER_IDLE, "isTcpNoDataCount": 20})

    async def async_set_local_group(self, config):
        self.calls.append("ezhi:join")
        if self.hang_ezhi_join:
            await asyncio.sleep(3600)
        if self.fail_ezhi_join and not self.ezhi_join_applies_then_fails:
            raise self.fail_ezhi_join
        self.ezhi.update(thirdLink="4", config=config, systemMode="1")
        self.cfg_ezhi = config
        if self.fail_ezhi_join:             # taken, but the answer never came
            raise self.fail_ezhi_join

    async def async_clear_local_group(self):
        self.calls.append("ezhi:leave")
        if self.fail_ezhi_leave:
            raise self.fail_ezhi_leave
        self.ezhi.update(thirdLink="0", config={})

    # meter side
    async def async_get_local_link(self):
        if self.fail_sem_read:
            raise self.fail_sem_read
        return dict(self.sem)

    async def async_set_local_link(self, enabled, config=None):
        self.calls.append("sem:join" if enabled else "sem:leave")
        if not enabled and self.fail_sem_leave and "sem:join" in self.calls[:-1] and self.fail_sem_leave == "always":
            raise EzhiCloudError("meter unreachable")
        if not enabled and self.fail_sem_leave == "once-after-ezhi" and "ezhi:leave" in self.calls:
            raise EzhiCloudError("meter unreachable")
        if enabled:
            self.sem = {"status": "1", "config": config}
            self.cfg_sem = config
        else:
            self.sem = dict(SEM_OFF)


class Clock:
    """Time that only moves when the controller sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def controller(world, clock=None, **kwargs):
    clock = clock or Clock()
    return LocalControl(world, world, EZHI, SEM, sleep=clock.sleep, monotonic=clock.monotonic, **kwargs)


def test_enabling_sets_the_meter_first_then_the_inverter_with_one_config():
    async def scenario():
        world = World()
        assert await controller(world).async_enable(45) is None
        assert world.calls == ["sem:join", "ezhi:join"]
        assert world.cfg_sem == world.cfg_ezhi
        assert world.cfg_ezhi["power"] == "45"
        assert world.cfg_ezhi["meter"] == SEM
        assert world.cfg_ezhi["device"] == {EZHI: "1.00"}
        assert len(world.cfg_ezhi["vrn"]) == 6

    asyncio.run(scenario())


def test_every_forced_enable_draws_a_new_vrn():
    async def scenario():
        world = World()
        ctl = controller(world)
        await ctl.async_enable(30)
        first = world.cfg_ezhi["vrn"]
        seen = {first}
        for _ in range(5):
            await ctl.async_enable(30, force=True)
            seen.add(world.cfg_ezhi["vrn"])
        assert len(seen) > 1

    asyncio.run(scenario())


def test_a_bad_offset_touches_nothing():
    async def scenario():
        world = World()
        with pytest.raises(LocalControlError):
            await controller(world).async_enable(500)
        assert world.calls == []

    asyncio.run(scenario())


def test_the_total_power_is_the_apps_1200_even_on_an_inverter_set_to_800_w():
    """The vendor app takes totalPower from a table of nominal powers (D02 1200 W)
    and never reads the power limit; the integration does the same."""
    async def scenario():
        world = World()
        world.ezhi["powerLimit"] = "800"
        await controller(world).async_enable(100)       # the app's 120 W cap stands
        for cfg in (world.cfg_sem, world.cfg_ezhi):
            assert cfg["totalPower"] == "1200" and cfg["totalPvPower"] == "1200"
        assert world.cfg_ezhi["power"] == "100"

    asyncio.run(scenario())


def test_switching_the_power_limit_does_not_make_a_standing_group_be_formed_again():
    async def scenario():
        world = World()
        ctl = controller(world)
        await ctl.async_enable(30)
        world.ezhi["powerLimit"] = "800"
        await ctl.async_enable(30)
        assert world.calls == ["sem:join", "ezhi:join"]

    asyncio.run(scenario())


def test_if_the_inverter_refuses_the_meter_is_taken_back_out():
    async def scenario():
        world = World()
        world.fail_ezhi_join = EzhiCloudError("rejected")
        with pytest.raises(EzhiCloudError, match="rejected"):
            await controller(world).async_enable(30)
        assert world.calls == ["sem:join", "ezhi:join", "sem:leave"]
        assert world.sem == SEM_OFF

    asyncio.run(scenario())


def test_if_taking_the_meter_back_out_fails_too_the_first_error_is_the_one_raised():
    async def scenario():
        world = World()
        world.fail_ezhi_join = EzhiCloudError("rejected")
        world.fail_sem_leave = "always"
        with pytest.raises(EzhiCloudError, match="rejected"):
            await controller(world).async_enable(30)

    asyncio.run(scenario())


def test_waiting_survives_the_inverters_reconnect_and_ends_on_the_first_good_read():
    async def scenario():
        world = World()
        world.reconnecting_reads = 3        # the first reads time out
        world.flowing_after = 5             # then the group exists but delivers nothing yet
        clock = Clock()
        state = await controller(world, clock).async_enable(30, wait=True)
        assert state.active and state.data_flowing
        assert clock.sleeps and all(s == lc.LINK_POLL_S for s in clock.sleeps)
        assert len(clock.sleeps) >= 3

    asyncio.run(scenario())


def test_waiting_gives_up_and_says_where_to_look():
    async def scenario():
        world = World()
        world.flowing_after = 10 ** 9       # never delivers
        clock = Clock()
        with pytest.raises(LocalControlError) as err:
            await controller(world, clock).async_enable(30, wait=True, link_wait=20)
        text = str(err.value)
        assert "same segment" in text and "3333" in text and "mDNS" in text
        assert "seconds without data: 20" in text
        assert clock.now >= 20

    asyncio.run(scenario())


def test_waiting_gives_up_even_if_every_read_fails():
    async def scenario():
        world = World()
        world.reconnecting_reads = 10 ** 9
        with pytest.raises(LocalControlError):
            await controller(world).async_enable(30, wait=True, link_wait=15)

    asyncio.run(scenario())


def test_disabling_dissolves_the_inverter_first_then_the_meter():
    async def scenario():
        world = World()
        ctl = controller(world)
        await ctl.async_enable(30)
        world.calls.clear()
        await ctl.async_disable()
        assert world.calls == ["ezhi:leave", "sem:leave"]
        assert world.ezhi["thirdLink"] == "0" and world.ezhi["config"] == {}
        assert world.sem == SEM_OFF

    asyncio.run(scenario())


def test_if_the_inverter_will_not_leave_the_meter_is_left_alone():
    """The inverter's output is what matters; do not half-undo the group on top
    of a failure and leave a state nobody asked for."""
    async def scenario():
        world = World()
        world.fail_ezhi_leave = EzhiCloudError("no answer")
        with pytest.raises(EzhiCloudError):
            await controller(world).async_disable()
        assert world.calls == ["ezhi:leave"]

    asyncio.run(scenario())


def test_a_meter_that_fails_to_leave_is_reported():
    async def scenario():
        world = World()
        world.fail_sem_leave = "once-after-ezhi"
        with pytest.raises(EzhiCloudError, match="meter unreachable"):
            await controller(world).async_disable()
        assert world.calls == ["ezhi:leave", "sem:leave"]

    asyncio.run(scenario())


def test_reading_the_state_needs_the_inverter_and_the_meter():
    async def scenario():
        world = World()
        world.ezhi, world.sem = dict(EZHI_ON), dict(SEM_ON)
        st = await controller(world).async_read_state()
        assert st.active

        world.reconnecting_reads = 10 ** 9
        with pytest.raises(EzhiCloudError):
            await controller(world).async_read_state()

    asyncio.run(scenario())


def test_a_failed_meter_status_read_does_not_fail_the_state():
    async def scenario():
        world = World()
        world.ezhi, world.sem = dict(EZHI_ON), dict(SEM_ON)
        world.fail_meter_read = True
        st = await controller(world).async_read_state()
        assert st.active and st.data_flowing is None

    asyncio.run(scenario())


def test_a_bug_in_a_read_is_not_swallowed():
    class Broken(World):
        async def async_get_local_link(self):
            raise TypeError("a bug, not a transport error")

    async def scenario():
        with pytest.raises(TypeError):
            await controller(Broken()).async_read_state()

    asyncio.run(scenario())


def test_two_quick_taps_do_not_interleave_their_commands():
    async def scenario():
        world = World()
        gate = asyncio.Event()
        original = world.async_set_local_link

        async def slow(enabled, config=None):
            if enabled:
                await gate.wait()
            await original(enabled, config)

        world.async_set_local_link = slow
        ctl = controller(world)
        first = asyncio.ensure_future(ctl.async_enable(30))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(ctl.async_disable())
        await asyncio.sleep(0)
        assert world.calls == []            # the meter's join is still held at the gate
        gate.set()
        await asyncio.gather(first, second)
        assert world.calls == ["sem:join", "ezhi:join", "ezhi:leave", "sem:leave"]

    asyncio.run(scenario())


# --- saying what is wrong ---------------------------------------------------------------

def test_a_working_or_absent_group_has_nothing_to_report():
    assert lc.describe_problem(lc.evaluate(EZHI_ON, SEM_ON, METER_OK, EZHI, SEM)) is None
    assert lc.describe_problem(lc.evaluate(EZHI_OFF, SEM_OFF, METER_IDLE, EZHI, SEM)) is None


def test_a_half_set_group_says_which_half():
    only_meter = lc.describe_problem(lc.evaluate(EZHI_OFF, SEM_ON, METER_IDLE, EZHI, SEM))
    assert "only the smart meter is in the group" in only_meter
    assert "inverter thirdLink 0" in only_meter and "meter localLink status 1" in only_meter
    assert "Switch Local Control off and on again" in only_meter
    only_inverter = lc.describe_problem(lc.evaluate(EZHI_ON, SEM_OFF, METER_IDLE, EZHI, SEM))
    assert "only the inverter is in the group" in only_inverter
    assert "inverter thirdLink 4" in only_inverter and "meter localLink status 0" in only_inverter


def test_each_kind_of_problem_has_its_own_code():
    def code(*args):
        problem = lc.problem_of(lc.evaluate(*args, EZHI, SEM))
        return None if problem is None else problem.code

    assert code(EZHI_ON, SEM_ON, METER_OK) is None
    assert code(EZHI_OFF, SEM_OFF, METER_IDLE) is None
    assert code(EZHI_ON, SEM_OFF, METER_IDLE) == "inverter_only"
    assert code(EZHI_OFF, SEM_ON, METER_IDLE) == "meter_only"
    assert code(EZHI_ON, {"config": {**CFG, "vrn": "1"}, "status": "1"}, METER_OK) == "mismatch"
    assert code(EZHI_ON, SEM_ON, {**METER_OK, "isTcpNoDataCount": "40"}) == "no_data"


def test_two_halves_that_disagree_say_so_and_name_the_field():
    other = {**CFG, "vrn": "999999"}
    state = lc.evaluate(EZHI_ON, {"config": other, "status": "1"}, METER_OK, EZHI, SEM)
    assert not state.consistent
    assert len(state.mismatch) == 1 and "version differs" in state.mismatch[0]
    assert "999999" in state.mismatch[0]
    text = lc.describe_problem(state)
    assert "not in the same one" in text and "999999" in text


def test_every_difference_between_the_halves_is_named():
    wrong_meter = {**CFG, "meter": "M99999999999"}
    no_inverter = {**CFG, "device": {"D11111111111": "1.00"}}
    st = lc.evaluate(
        {**EZHI_ON, "config": no_inverter},
        {"config": wrong_meter, "status": "1"}, METER_OK, EZHI, SEM)
    joined = " | ".join(st.mismatch)
    assert "does not list the inverter" in joined and EZHI in joined
    assert "the meter's group names the meter 'M99999999999'" in joined
    # An empty config says so too, rather than "do not match".
    empty = lc.evaluate({**EZHI_ON, "config": {}}, SEM_ON, METER_OK, EZHI, SEM)
    assert "the inverter holds no group configuration" in empty.mismatch


def test_a_number_and_its_string_are_the_same_value():
    """5 and "5" mean the same group version; a firmware that answers one way
    on one device and the other way on the other must not read as a mismatch."""
    as_number = {**CFG, "vrn": int(CFG["vrn"])}
    st = lc.evaluate(EZHI_ON, {"config": as_number, "status": "1"}, METER_OK, EZHI, SEM)
    assert st.consistent and st.mismatch == ()


def test_the_raw_membership_values_are_kept_for_the_sensor():
    st = lc.evaluate(EZHI_ON, SEM_ON, METER_OK, EZHI, SEM)
    assert (st.third_link, st.sem_status) == ("4", "1")
    gone = lc.evaluate({}, {}, None, EZHI, SEM)
    assert (gone.third_link, gone.sem_status) == (None, None)


def test_when_both_devices_are_silent_both_are_named():
    async def scenario():
        world = World()
        world.fail_ezhi_read = EzhiCloudError("the inverter did not answer systemMode within 10 s")
        world.fail_sem_read = EzhiCloudError("the smart meter did not answer localLink within 10 s")
        with pytest.raises(LocalControlError) as err:
            await controller(world).async_read_state()
        assert "the inverter did not answer" in str(err.value)
        assert "the smart meter did not answer" in str(err.value)

    asyncio.run(scenario())


def test_the_read_error_names_the_devices_that_did_not_answer():
    async def scenario():
        cases = (
            (True, False, ("inverter",)),
            (False, True, ("meter",)),
            (True, True, ("inverter", "meter")),
        )
        for ezhi_fails, sem_fails, expected in cases:
            world = World()
            if ezhi_fails:
                world.fail_ezhi_read = EzhiCloudError(
                    "the inverter did not answer read systemMode within 12 s")
            if sem_fails:
                world.fail_sem_read = EzhiCloudError(
                    "the smart meter did not answer read localLink within 12 s")
            with pytest.raises(lc.LocalControlReadError) as err:
                await controller(world).async_read_state()
            assert err.value.silent == expected
            assert isinstance(err.value, LocalControlError)       # callers catch this family

    asyncio.run(scenario())


def test_an_unreadable_group_names_the_silent_device():
    def problem(*silent):
        return lc.unreadable_problem(lc.LocalControlReadError("timeout", silent))

    assert problem("inverter").summary == "the inverter did not answer"
    assert "the inverter is powered" in problem("inverter").hint
    assert problem("meter").summary == "the smart meter did not answer"
    assert "the smart meter is powered" in problem("meter").hint
    both = problem("inverter", "meter")
    assert both.summary == "neither the inverter nor the smart meter answered"
    assert "both are powered" in both.hint
    # An error that does not know falls back to naming the pair, as before.
    unknown = lc.unreadable_problem(EzhiCloudError("timeout"))
    assert unknown.summary == "the inverter or the smart meter did not answer"
    assert unknown.devices == ()
    assert problem("meter").code == "unreadable" and problem("meter").devices == ("meter",)


def test_the_status_names_the_state_in_one_word():
    def status(*args, settling=False):
        state = lc.evaluate(*args, EZHI, SEM)
        return lc.status_of(state, lc.problem_of(state, settling), settling)

    assert status(EZHI_OFF, SEM_OFF, METER_IDLE) == "off"
    assert status(EZHI_ON, SEM_ON, METER_OK) == "regulating"
    assert status(EZHI_ON, SEM_OFF, METER_IDLE) == "inverter_only"
    assert status(EZHI_OFF, SEM_ON, METER_IDLE) == "meter_only"
    assert status(EZHI_ON, {"config": {**CFG, "vrn": "1"}, "status": "1"}, METER_OK) == "mismatch"
    silent = {**METER_OK, "isTcpNoDataCount": "40"}
    assert status(EZHI_ON, SEM_ON, silent) == "no_data"
    # While a command is still taking effect, no readings yet is not a fault.
    assert status(EZHI_ON, SEM_ON, silent, settling=True) == "starting"
    # Unknown data flow (meterStatus unreadable) is not held against a standing group.
    assert status(EZHI_ON, SEM_ON, None) == "regulating"
    assert lc.status_of(None, None) is None


def test_an_unreadable_group_is_named_by_the_device_that_is_silent():
    def status(*silent):
        problem = lc.unreadable_problem(lc.LocalControlReadError("x", silent))
        return lc.status_of(None, problem)

    assert status("inverter") == "inverter_silent"
    assert status("meter") == "meter_silent"
    assert status("inverter", "meter") == "both_silent"
    assert status() == "unreadable"


def test_every_problem_code_and_every_status_is_in_the_list_of_options():
    codes = {lc.PROBLEM_INVERTER_ONLY, lc.PROBLEM_METER_ONLY, lc.PROBLEM_MISMATCH,
             lc.PROBLEM_NO_DATA, lc.PROBLEM_UNREADABLE}
    assert codes <= set(lc.STATUS_OPTIONS)
    assert {lc.STATUS_OFF, lc.STATUS_STARTING, lc.STATUS_REGULATING,
            lc.STATUS_INVERTER_SILENT, lc.STATUS_METER_SILENT,
            lc.STATUS_BOTH_SILENT} <= set(lc.STATUS_OPTIONS)
    assert len(lc.STATUS_OPTIONS) == len(set(lc.STATUS_OPTIONS))


def test_a_silent_meter_points_at_the_network():
    st = lc.evaluate(EZHI_ON, SEM_ON, {**METER_OK, "isTcpNoDataCount": "40"}, EZHI, SEM)
    text = lc.describe_problem(st)
    assert "40 s" in text and "mDNS" in text and "3333" in text


# --- idempotence ------------------------------------------------------------------------

def test_enabling_a_group_that_already_stands_with_that_offset_sends_nothing():
    """A new version makes the inverter reconnect and stop regulating for ~40 s;
    an automation that re-asserts "on" must not cost that every time."""
    async def scenario():
        world = World()
        ctl = controller(world)
        await ctl.async_enable(30)
        world.calls.clear()
        assert await ctl.async_enable(30) is None
        assert world.calls == []

    asyncio.run(scenario())


def test_a_different_offset_or_force_forms_the_group_again():
    async def scenario():
        world = World()
        ctl = controller(world)
        await ctl.async_enable(30)
        world.calls.clear()
        await ctl.async_enable(60)
        assert world.calls == ["sem:join", "ezhi:join"] and world.cfg_ezhi["power"] == "60"
        world.calls.clear()
        await ctl.async_enable(60, force=True)
        assert world.calls == ["sem:join", "ezhi:join"]

    asyncio.run(scenario())


def test_an_idempotent_enable_still_waits_when_asked_to():
    async def scenario():
        world = World()
        ctl = controller(world)
        await ctl.async_enable(30)
        world.calls.clear()
        state = await ctl.async_enable(30, wait=True)
        assert state.active and world.calls == []

    asyncio.run(scenario())


def test_devices_that_do_not_answer_the_look_ahead_do_not_stop_the_attempt():
    async def scenario():
        world = World()
        world.reconnecting_reads = 1            # the pre-read times out
        await controller(world).async_enable(30)
        assert world.calls == ["sem:join", "ezhi:join"]

    asyncio.run(scenario())


# --- a failed attempt leaves a pair that agrees ---------------------------------------------

def test_a_failed_offset_change_leaves_the_working_group_as_it_was():
    """The meter took the new config, the inverter refused: the meter must go
    back to the old one, not out of the group."""
    async def scenario():
        world = World()
        ctl = controller(world)
        await ctl.async_enable(30)
        before = dict(world.cfg_ezhi)
        world.fail_ezhi_join = EzhiCloudError("rejected")
        with pytest.raises(EzhiCloudError, match="rejected"):
            await ctl.async_enable(60)
        assert world.sem == {"status": "1", "config": before}
        assert world.ezhi["config"] == before
        state = await ctl.async_read_state()
        assert state.active and state.offset == 30

    asyncio.run(scenario())


def test_a_failed_first_attempt_does_not_touch_the_inverter():
    """Nothing was joined, so nothing is dissolved: a 'leave' would also switch
    the inverter to Balcony Storage mode for no reason."""
    async def scenario():
        world = World()
        world.fail_ezhi_join = EzhiCloudError("rejected")
        with pytest.raises(EzhiCloudError):
            await controller(world).async_enable(30)
        assert "ezhi:leave" not in world.calls
        assert world.calls == ["sem:join", "ezhi:join", "sem:leave"]

    asyncio.run(scenario())


def test_an_answer_that_was_lost_after_the_inverter_took_the_group_is_not_undone():
    async def scenario():
        world = World()
        world.fail_ezhi_join = EzhiCloudError("did not answer")
        world.ezhi_join_applies_then_fails = True
        with pytest.raises(EzhiCloudError, match="did not answer"):
            await controller(world).async_enable(30)
        assert world.calls == ["sem:join", "ezhi:join"]          # no 'leave' of anything
        state = await controller(world).async_read_state()
        assert state.active

    asyncio.run(scenario())


def test_a_deadline_that_cancels_the_attempt_still_takes_the_meter_back():
    async def scenario():
        world = World()
        world.hang_ezhi_join = True
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(controller(world).async_enable(30), 0.05)
        assert world.calls == ["sem:join", "ezhi:join", "sem:leave"]
        assert world.sem == SEM_OFF

    asyncio.run(scenario())


def test_when_the_inverter_cannot_be_asked_either_the_meter_is_still_taken_back():
    async def scenario():
        world = World()
        world.fail_ezhi_join = EzhiCloudError("rejected")
        original = world.async_get_config
        n = {"reads": 0}

        async def flaky():
            n["reads"] += 1
            if n["reads"] > 1:                  # the look-ahead answers, the check afterwards does not
                raise EzhiCloudError("timeout")
            return await original()

        world.async_get_config = flaky
        with pytest.raises(EzhiCloudError, match="rejected"):
            await controller(world).async_enable(30)
        assert world.calls[-1] == "sem:leave"

    asyncio.run(scenario())


# --- what the last command says about the inverter ---------------------------------------------

def test_the_last_command_outranks_the_poll_for_a_while():
    async def scenario():
        world = World()
        clock = Clock()
        ctl = controller(world, clock)
        not_in_group = lc.evaluate(EZHI_OFF, SEM_OFF, METER_IDLE, EZHI, SEM)
        in_group = lc.evaluate(EZHI_ON, SEM_ON, METER_OK, EZHI, SEM)
        assert not ctl.inverter_may_be_grouped(not_in_group)
        assert ctl.inverter_may_be_grouped(in_group)
        assert not ctl.inverter_may_be_grouped(None)

        await ctl.async_enable(30)
        assert ctl.inverter_may_be_grouped(not_in_group)        # the poll lags behind
        clock.now += lc.INTENT_FOR_S + 1
        assert not ctl.inverter_may_be_grouped(not_in_group)    # then the poll decides again

        await ctl.async_enable(30)
        await ctl.async_disable()
        assert not ctl.inverter_may_be_grouped(in_group)        # a stale poll must not say otherwise

    asyncio.run(scenario())


# --- waiting when the data flow cannot be read ------------------------------------------------

def test_waiting_says_so_when_the_group_is_set_but_the_data_flow_cannot_be_read():
    async def scenario():
        world = World()
        world.fail_meter_read = True
        with pytest.raises(LocalControlError, match="meterStatus could not be read"):
            await controller(world).async_enable(30, wait=True, link_wait=10)

    asyncio.run(scenario())


# --- no alarm while the group settles --------------------------------------------------------

def test_no_readings_yet_is_normal_right_after_a_command_but_a_broken_group_is_not():
    silent = lc.evaluate(EZHI_ON, SEM_ON, {**METER_OK, "isTcpNoDataCount": 20}, EZHI, SEM)
    assert lc.describe_problem(silent) is not None
    assert lc.describe_problem(silent, settling=True) is None
    half = lc.evaluate(EZHI_OFF, SEM_ON, METER_IDLE, EZHI, SEM)
    assert lc.describe_problem(half, settling=True) is not None

"""The Home Assistant layer of Local Control, on a bare Home Assistant core.

Like test_mqtt_connect.py this needs the real `homeassistant` package. It runs
a HomeAssistant object with no integrations loaded: enough for coordinators,
the entity registry and entity objects, which is where the wiring lives. What
it does not do is set up a config entry end to end -- there is no test harness
for that in this repository, and the logic the entry setup calls is covered
where it lives (test_local_control.py, test_sem_feed.py, test_ntp_responder.py).

Imports go through the real package (custom_components.apsystems_ezhi_local),
not the ezhi_component shim: the platform modules import from the package root.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("homeassistant")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant.config_entries import ConfigEntry  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.config_entries import ConfigEntries  # noqa: E402
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame  # noqa: E402

from custom_components.apsystems_ezhi_local import (  # noqa: E402
    ApSystemsCloudCoordinator,
    ApSystemsDataCoordinator,
    _remove_smart_linking_entity,
    lc_runtime as rt,
)
from custom_components.apsystems_ezhi_local import (  # noqa: E402
    binary_sensor as bs,
    number as num,
    select as sel,
    sensor as sens,
    switch as sw,
)
from custom_components.apsystems_ezhi_local.cloud import EzhiCloudError, control_config  # noqa: E402
from custom_components.apsystems_ezhi_local.const import (  # noqa: E402
    CONF_ANSWER_NTP,
    CONF_CONTROL_TRANSPORT,
    CONF_LOCAL_CONTROL_OFFSET,
    CONF_SEM_DEVICE_ID,
    CONTROL_GRACE_S,
    DOMAIN,
    HTTP_GRACE_S,
    LC_COORDINATOR,
    RECONNECT_GRACE_S,
    TRANSPORT_LOCAL_MQTT,
)
from custom_components.apsystems_ezhi_local.entity import (  # noqa: E402
    async_register_sem_device,
    async_require_local_mode,
    extend_grace,
    local_control_holds_inverter,
)
from custom_components.apsystems_ezhi_local.grace import Grace  # noqa: E402
from custom_components.apsystems_ezhi_local.local_control import (  # noqa: E402
    STATUS_OPTIONS,
    LocalControlReadError,
    LocalControlState,
)
from custom_components.apsystems_ezhi_local.sem_feed import SemFeed  # noqa: E402

EZHI = "D00000000000"
SEM = "M00000000000"
NAME = "EZHI"


# --- helpers -------------------------------------------------------------------------

def run(coro_fn):
    """Run `coro_fn(hass)` on a fresh bare Home Assistant."""
    async def scenario():
        config_dir = tempfile.mkdtemp()
        hass = HomeAssistant(config_dir)
        hass.config.config_dir = config_dir
        hass.config.time_zone = "Europe/Berlin"
        frame.async_setup(hass)          # what Home Assistant's own bootstrap does
        return await coro_fn(hass)

    return asyncio.run(scenario())


def make_entry(**data) -> ConfigEntry:
    return ConfigEntry(
        domain=DOMAIN, title=NAME, data=data, options={}, version=1,
        minor_version=1, source="user", unique_id=None, discovery_keys={},
        subentries_data=None,
    )


def state(*, ezhi=True, sem=True, consistent=True, offset=30, no_data=2, power=30.0,
          mismatch=()):
    return LocalControlState(
        ezhi_member=ezhi, sem_member=sem, consistent=consistent, offset=offset,
        vrn="123456", no_data_count=no_data, meter_power=power,
        third_link="4" if ezhi else "0", sem_status="1" if sem else "0",
        mismatch=tuple(mismatch),
    )


ON = state()
OFF = state(ezhi=False, sem=False, consistent=False, offset=None, no_data=5, power=None)


class FakeControl:
    """Stands in for LocalControl: a script of reads, a record of commands."""

    def __init__(self, reads=(ON,)):
        self.reads = list(reads)
        self.enabled: list[tuple] = []
        self.disabled = 0
        self.waited = 0
        self.fail: Exception | None = None
        self.wait_fail: Exception | None = None

    async def async_read_state(self):
        item = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def async_enable(self, offset, *, wait=False, link_wait=0):
        if self.fail:
            raise self.fail
        self.enabled.append((offset, wait))

    async def async_disable(self):
        if self.fail:
            raise self.fail
        self.disabled += 1

    async def async_wait_until_working(self, link_wait=0):
        if self.wait_fail:
            raise self.wait_fail
        self.waited += 1

    def inverter_may_be_grouped(self, state):
        return bool(state is not None and state.ezhi_member)


def lc_coordinator(hass, control, offset=30, entry=None):
    return rt.LocalControlCoordinator(hass, entry or make_entry(), control, offset)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def nosub(_topic, _handler):
    return lambda: None


# --- the group coordinator ----------------------------------------------------------

def test_a_read_becomes_the_coordinators_data():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl([ON]))
        await c.async_refresh()
        assert c.last_update_success and c.data == ON

    run(go)


def test_the_first_reads_that_fail_are_quiet_and_tried_again_soon(caplog):
    """After a start or a reload the devices are often still reconnecting: no
    problem and no error yet, and another try in seconds, not in half a minute."""
    async def go(hass):
        err = EzhiCloudError("the smart meter did not answer localLink within 12 s")
        c = lc_coordinator(hass, FakeControl([err, err, ON]))
        with caplog.at_level("WARNING", logger=rt._LOGGER.name):
            await c.async_refresh()
            assert c.last_update_success and c.data is None      # nothing known yet
            assert c.problem is None and c.status is None
            assert c.update_interval.total_seconds() == rt.LC_FAST_POLL_S
            await c.async_refresh()
            assert c.last_update_success and c.problem is None
        assert not caplog.records                                # no error, no warning
        await c.async_refresh()
        assert c.data == ON and c.status == "regulating"
        assert c.update_interval.total_seconds() == rt.LC_POLL_S

    run(go)


def test_three_failed_first_reads_make_the_entities_unavailable():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl([EzhiCloudError("timeout")]))
        for _ in range(rt.LC_READ_FAILURES_TOLERATED):
            await c.async_refresh()
            assert c.last_update_success
        await c.async_refresh()
        assert not c.last_update_success
        assert c.problem.code == "unreadable"
        assert c.update_interval.total_seconds() == rt.LC_POLL_S  # not hammered every 5 s

    run(go)


def test_a_failed_read_right_after_a_change_keeps_the_last_state(monkeypatch):
    """The inverter reconnects for ~11 s after accepting a group; reads time out
    meanwhile, and that must not flash 'unavailable' across the entities."""
    async def go(hass):
        control = FakeControl([ON, EzhiCloudError("timeout")])
        c = lc_coordinator(hass, control)
        await c.async_refresh()
        c.speed_up()
        await c.async_refresh()
        assert c.last_update_success and c.data == ON

    run(go)


def test_the_settle_window_ends_and_the_normal_interval_returns(monkeypatch):
    async def go(hass):
        clock = Clock()
        monkeypatch.setattr(rt, "time", types.SimpleNamespace(monotonic=clock))
        control = FakeControl([ON, EzhiCloudError("timeout")])
        c = lc_coordinator(hass, control)
        await c.async_refresh()
        c.speed_up()
        assert c.update_interval.total_seconds() == rt.LC_FAST_POLL_S
        clock.now += rt.LC_FAST_FOR_S + 1
        for _ in range(rt.LC_READ_FAILURES_TOLERATED):
            await c.async_refresh()
            assert c.last_update_success         # a lost read or two is a hiccup
        await c.async_refresh()                  # window over, and again: it counts now
        assert not c.last_update_success
        assert c.update_interval.total_seconds() == rt.LC_POLL_S

    run(go)


def test_a_lost_read_or_two_is_a_hiccup_the_third_in_a_row_is_not():
    async def go(hass):
        err = EzhiCloudError("the smart meter did not answer localLink within 10 s")
        control = FakeControl([ON, err, err, ON, err, err, err])
        c = lc_coordinator(hass, control)
        await c.async_refresh()
        for _ in range(2):
            await c.async_refresh()
            assert c.last_update_success and c.data == ON and c.problem is None
        await c.async_refresh()                  # a good read: the count starts over
        for _ in range(2):
            await c.async_refresh()
            assert c.last_update_success
        await c.async_refresh()                  # third failure in a row
        assert not c.last_update_success
        assert c.problem.code == "unreadable"
        assert "did not answer localLink" in c.problem.text

    run(go)


def test_a_problem_is_logged_when_it_appears_and_when_it_goes(caplog):
    async def go(hass):
        broken = state(sem=False, consistent=False)
        c = lc_coordinator(hass, FakeControl([ON, broken, broken, ON]))
        with caplog.at_level("INFO", logger=rt._LOGGER.name):
            await c.async_refresh()
            assert c.last_problem is None and not caplog.records
            await c.async_refresh()
            await c.async_refresh()              # the same problem again: no second line
            warnings = [r for r in caplog.records if r.levelname == "WARNING"]
            assert len(warnings) == 1
            assert "only the inverter is in the group" in warnings[0].getMessage()
            assert c.last_problem.code == "inverter_only" and c.last_problem_at is not None
            await c.async_refresh()
            assert c.problem is None
            assert "is gone" in caplog.records[-1].getMessage()
            assert c.last_problem is not None    # what it was stays on record

    run(go)


def test_the_end_of_the_settle_window_wakes_the_entities(monkeypatch):
    """An unchanged read does not notify (always_update=False), yet the verdict
    "no readings yet" turns into a problem the moment the window ends."""
    async def go(hass):
        clock = Clock()
        monkeypatch.setattr(rt, "time", types.SimpleNamespace(monotonic=clock))
        c = lc_coordinator(hass, FakeControl([state(no_data=40)]))
        woken = []
        c.async_add_listener(lambda: woken.append(1))
        await c.async_refresh()
        c.speed_up()
        woken.clear()
        clock.now += rt.LC_FAST_FOR_S + 1
        await c.async_refresh()
        assert woken and c.problem.code == "no_data"

    run(go)


# --- the meter coordinator ------------------------------------------------------------

def test_the_meter_has_nothing_until_it_reports_and_then_pushes():
    async def go(hass):
        clock = Clock()
        feed = SemFeed(SEM, nosub, min_interval=0.0, clock=clock)
        await feed.async_start()
        c = rt.SemCoordinator(hass, make_entry(), feed)
        assert c.data is None
        await c.async_refresh()
        assert c.last_update_success and c.data == {}     # nothing yet: unknown, not an error
        feed._on_event('{"identifier":"outputDataSecond","data":{"p":"12.5000"}}')
        assert c.last_update_success and c.data == {"p": 12.5}
        await c.async_shutdown()

    run(go)


def test_a_meter_that_never_reports_is_reported_after_a_minute(monkeypatch):
    async def go(hass):
        clock = Clock()
        monkeypatch.setattr(rt, "time", types.SimpleNamespace(monotonic=clock))
        feed = SemFeed(SEM, nosub, min_interval=0.0, clock=clock)
        await feed.async_start()
        c = rt.SemCoordinator(hass, make_entry(), feed)
        await c.async_refresh()
        assert c.last_update_success              # the first seconds after a start
        clock.now += rt.SEM_STALE_S + 1
        await c.async_refresh()
        assert not c.last_update_success          # a meter that stays silent is a fault
        await c.async_shutdown()

    run(go)


def test_a_meter_that_goes_quiet_turns_unavailable_and_recovers():
    async def go(hass):
        clock = Clock()
        feed = SemFeed(SEM, nosub, min_interval=0.0, clock=clock)
        await feed.async_start()
        c = rt.SemCoordinator(hass, make_entry(), feed)
        feed._on_event('{"identifier":"outputDataSecond","data":{"p":"5.0000"}}')
        clock.now += rt.SEM_STALE_S - 1
        await c.async_refresh()
        assert c.last_update_success              # 16 s gaps are normal; a minute is not
        clock.now += 5
        await c.async_refresh()
        assert not c.last_update_success
        feed._on_event('{"identifier":"outputDataSecond","data":{"p":"6.0000"}}')
        assert c.last_update_success and c.data["p"] == 6.0
        await c.async_shutdown()

    run(go)


def test_shutting_the_meter_coordinator_down_removes_its_listener():
    async def go(hass):
        feed = SemFeed(SEM, nosub, min_interval=0.0)
        c = rt.SemCoordinator(hass, make_entry(), feed)
        assert feed._listeners
        await c.async_shutdown()
        assert not feed._listeners

    run(go)


# --- the switch ---------------------------------------------------------------------------

def make_switch(hass, control, offset=30):
    c = lc_coordinator(hass, control, offset)
    c.data = OFF
    switch = sw.LocalControlSwitch(c, NAME)
    switch.hass = hass
    switch.async_write_ha_state = lambda: None     # not added to a platform
    return c, switch


def test_the_switch_reports_the_state_of_the_group():
    async def go(hass):
        c, switch = make_switch(hass, FakeControl())
        assert switch.is_on is False
        c.data = ON
        assert switch.is_on is True
        c.data = state(sem=False, consistent=False)    # half a group is not "on"
        assert switch.is_on is False
        c.data = None
        assert switch.is_on is None

    run(go)


def test_turning_on_sends_the_stored_offset_without_waiting():
    async def go(hass):
        control = FakeControl([OFF])
        c, switch = make_switch(hass, control, offset=45)
        await switch.async_turn_on()
        assert control.enabled == [(45, False)]
        assert c.update_interval.total_seconds() == rt.LC_FAST_POLL_S

    run(go)


def test_the_switch_shows_what_was_asked_for_until_the_devices_confirm():
    async def go(hass):
        c, switch = make_switch(hass, FakeControl([OFF]))
        await switch.async_turn_on()
        assert c.data == OFF
        assert switch.is_on is True                # the read still says off: no flip back
        c.data = ON
        assert switch.is_on is True and switch._pending is None

    run(go)


def test_the_asked_for_state_expires(monkeypatch):
    async def go(hass):
        clock = Clock()
        monkeypatch.setattr(sw, "time", types.SimpleNamespace(monotonic=clock))
        c, switch = make_switch(hass, FakeControl([OFF]))
        await switch.async_turn_on()
        clock.now += switch._PENDING_FOR_S + 1
        assert switch.is_on is False               # the devices never confirmed: say so

    run(go)


def test_turning_off_dissolves_the_group():
    async def go(hass):
        control = FakeControl([ON])
        c, switch = make_switch(hass, control)
        c.data = ON
        await switch.async_turn_off()
        assert control.disabled == 1
        assert switch.is_on is False

    run(go)


def test_a_refused_command_reaches_the_user_and_leaves_no_asked_for_state():
    async def go(hass):
        control = FakeControl([OFF])
        control.fail = EzhiCloudError("meter unreachable")
        c, switch = make_switch(hass, control)
        with pytest.raises(HomeAssistantError, match="meter unreachable"):
            await switch.async_turn_on()
        assert switch._pending is None and switch.is_on is False

    run(go)


def test_the_switch_attributes_carry_the_diagnosis():
    async def go(hass):
        c, switch = make_switch(hass, FakeControl())
        c.data = ON
        attrs = switch.extra_state_attributes
        assert attrs["offset_active"] == 30 and attrs["problem"] is None
        assert attrs["last_read_failed"] is False
        assert attrs["seconds_without_meter_data"] == 2

    run(go)


def test_there_is_no_smart_linking_switch_any_more():
    assert not hasattr(sw, "EzhiCloudThirdLinkSwitch")


# --- the offset number ------------------------------------------------------------------

def make_number(hass, control, entry=None, offset=30):
    c = lc_coordinator(hass, control, offset, entry)
    c.data = OFF
    number = num.LocalControlOffsetNumber(c, NAME)
    number.hass = hass
    hass.config_entries = MagicMock()
    return c, number


def test_the_offset_range_is_the_apps():
    async def go(hass):
        _c, number = make_number(hass, FakeControl())
        assert number.native_min_value == 0
        assert number.native_max_value == 120
        assert number.native_unit_of_measurement == "W"

    run(go)


def test_the_offset_shows_the_running_groups_value_else_the_stored_one():
    async def go(hass):
        c, number = make_number(hass, FakeControl(), offset=40)
        assert number.native_value == 40
        c.data = state(offset=55)
        assert number.native_value == 55           # changed in the vendor app, say

    run(go)


def test_without_a_group_the_offset_is_only_stored():
    async def go(hass):
        control = FakeControl([OFF])
        entry = make_entry(**{CONF_LOCAL_CONTROL_OFFSET: 30})
        c, number = make_number(hass, control, entry)
        await number.async_set_native_value(60)
        assert control.enabled == []
        assert c.offset == 60
        _args, kwargs = hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_LOCAL_CONTROL_OFFSET] == 60

    run(go)


def test_with_a_group_the_new_offset_is_applied_to_it():
    async def go(hass):
        control = FakeControl([ON])
        c, number = make_number(hass, control)
        c.data = ON
        await number.async_set_native_value(70)
        assert control.enabled == [(70, False)]
        assert c.offset == 70

    run(go)


def test_an_offset_the_app_would_refuse_is_refused_and_not_stored():
    async def go(hass):
        control = FakeControl([ON])
        c, number = make_number(hass, control)
        c.data = ON
        for bad in (-1, 121):
            with pytest.raises(HomeAssistantError, match="outside 0"):
                await number.async_set_native_value(bad)
        assert control.enabled == [] and c.offset == 30

    run(go)


def test_a_rejected_apply_does_not_change_the_stored_offset():
    async def go(hass):
        control = FakeControl([ON])
        control.fail = EzhiCloudError("inverter did not answer")
        c, number = make_number(hass, control)
        c.data = ON
        with pytest.raises(HomeAssistantError):
            await number.async_set_native_value(70)
        assert c.offset == 30
        hass.config_entries.async_update_entry.assert_not_called()

    run(go)


# --- what Local Control takes over -------------------------------------------------------

def test_the_inverter_is_held_only_while_it_is_in_the_group():
    c = types.SimpleNamespace(data=ON, control=FakeControl())
    assert local_control_holds_inverter({LC_COORDINATOR: c})
    c.data = state(ezhi=False)                     # only the meter's half: nothing to protect
    assert not local_control_holds_inverter({LC_COORDINATOR: c})
    c.data = None                                  # not read yet: never block on a guess
    assert not local_control_holds_inverter({LC_COORDINATOR: c})
    assert not local_control_holds_inverter({})     # Local Control not set up


class CloudApiStub:
    def __init__(self):
        self.calls = []

    async def async_set_system_mode(self, **changes):
        self.calls.append(changes)


def cloud_stub():
    api = CloudApiStub()

    async def refresh():
        return None

    applied: list = []
    return types.SimpleNamespace(
        data=None, api=api, async_request_refresh=refresh,
        apply_config=applied.append, applied=applied), api


def test_the_preset_power_is_refused_while_the_group_stands_but_the_discharge_floor_is_not():
    async def go(hass):
        held = {LC_COORDINATOR: types.SimpleNamespace(data=ON, control=FakeControl())}
        coordinator, api = cloud_stub()
        preset = num.EzhiCloudSystemModeNumber(
            coordinator, NAME, num.SYSTEM_MODE_NUMBERS["userSetPower"], held)
        floor = num.EzhiCloudSystemModeNumber(
            coordinator, NAME, num.SYSTEM_MODE_NUMBERS["dischargeProtection"], held)
        with pytest.raises(HomeAssistantError, match="Local Control is active"):
            await preset.async_set_native_value(300)
        assert api.calls == []
        await floor.async_set_native_value(20)
        assert api.calls == [{"dischargeProtection": 20}]

    run(go)


def test_the_system_mode_is_refused_while_the_group_stands():
    async def go(hass):
        held = {LC_COORDINATOR: types.SimpleNamespace(data=ON, control=FakeControl())}
        coordinator, api = cloud_stub()
        select = sel.EzhiCloudSystemModeSelect(coordinator, NAME, held)
        with pytest.raises(HomeAssistantError, match="Local Control is active"):
            await select.async_select_option("Local")
        assert api.calls == []
        free = sel.EzhiCloudSystemModeSelect(coordinator, NAME, {})
        await free.async_select_option("Local")
        assert api.calls == [{"systemMode": "4"}]
        # The select shows what was set while the inverter may be silent.
        assert coordinator.applied == [{"systemMode": "4"}]

    run(go)


# --- the sensors --------------------------------------------------------------------------

def sem_sensor(key):
    field = next(f for f in sens.SEM_SENSOR_FIELDS if f.key == key)
    coordinator = types.SimpleNamespace(data={"p": 12.5, "p1": 4.0, "iE": 1000.0})
    return sens.SemSensor(coordinator, NAME, SEM, field)


def test_the_meter_sensors_read_the_pushed_values_with_the_right_classes():
    power = sem_sensor("p")
    assert power.native_value == 12.5
    assert power.native_unit_of_measurement == "W"
    assert power.device_class == "power" and power.state_class == "measurement"
    assert power.entity_registry_enabled_default is True
    assert sem_sensor("p2").native_value is None       # not reported (yet): unknown


def test_the_energy_counters_are_on_by_default_in_kwh_for_the_energy_dashboard():
    """Import and export are what Home Assistant's energy dashboard asks for
    (grid consumption, return to grid); hiding them defeated the meter."""
    for key in ("iE", "iE1", "iE2", "iE3", "eE", "eE1", "eE2", "eE3"):
        energy = sem_sensor(key)
        assert energy.entity_registry_enabled_default is True, key
        assert energy.native_unit_of_measurement == "kWh"
        assert energy.device_class == "energy"
        assert energy.state_class == "total_increasing"


def test_import_and_export_are_named_as_a_pair():
    names = {f.key: f.name for f in sens.SEM_SENSOR_FIELDS}
    assert names["iE"] == "Grid Import Energy" and names["eE"] == "Grid Export Energy"
    assert names["eE2"] == "Grid Export Energy L2"
    assert len(sens.SEM_SENSOR_FIELDS) == len({f.key for f in sens.SEM_SENSOR_FIELDS})


def test_the_meter_is_its_own_device():
    info = sem_sensor("p").device_info
    assert (DOMAIN, f"sem_{SEM}") in info["identifiers"]
    assert info["model"] == "SEM"
    # Home Assistant retires the identifier pair; the link is made by registry id.
    assert "via_device" not in info
    assert sem_sensor("p").unique_id == f"apsystems_sem_{SEM}_p"


def test_the_meter_is_attached_to_the_inverter_by_registry_id():
    async def go(hass):
        hass.config_entries = ConfigEntries(hass, {})
        entry = make_entry()
        hass.config_entries._entries[entry.entry_id] = entry
        await dr.async_load(hass)
        async_register_sem_device(hass, entry, NAME, SEM)
        async_register_sem_device(hass, entry, NAME, SEM)        # again: nothing changes
        registry = dr.async_get(hass)
        inverter = registry.async_get_device(identifiers={(DOMAIN, NAME)})
        meter = registry.async_get_device(identifiers={(DOMAIN, f"sem_{SEM}")})
        assert meter.via_device_id == inverter.id
        assert meter.serial_number == SEM and meter.model == "SEM"
        assert len(registry.devices) == 2

    run(go)


def test_the_problem_sensor_is_on_only_for_a_group_that_was_asked_for_and_fails():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl())
        sensor = bs.LocalControlProblemSensor(c, NAME)
        c.data = None
        assert sensor.is_on is None
        c.data = ON
        assert sensor.is_on is False and sensor.extra_state_attributes == {}
        c.data = OFF
        assert sensor.is_on is False                  # off is not a fault
        c.data = state(sem=False, consistent=False)
        assert sensor.is_on is True
        attrs = sensor.extra_state_attributes
        assert "only the inverter is in the group" in attrs["reason"]
        assert attrs["cause"] == "inverter_only"
        assert attrs["inverter_third_link"] == "4" and attrs["meter_link_status"] == "0"
        assert attrs["inverter_in_group"] is True and attrs["meter_in_group"] is False
        c.data = state(no_data=40)
        assert sensor.is_on is True
        assert "40 s" in sensor.extra_state_attributes["reason"]
        assert sensor.extra_state_attributes["cause"] == "no_data"
        assert sensor.extra_state_attributes["seconds_without_meter_data"] == 40

    run(go)


def test_the_problem_sensor_lists_what_differs_between_the_two_halves():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl())
        sensor = bs.LocalControlProblemSensor(c, NAME)
        c.data = state(consistent=False,
                       mismatch=["the group version differs: inverter '1', meter '2'"])
        attrs = sensor.extra_state_attributes
        assert attrs["cause"] == "mismatch"
        assert attrs["differences"] == ["the group version differs: inverter '1', meter '2'"]
        assert "version differs" in attrs["reason"]

    run(go)


def logbook_sensor(hass, c):
    """A Problem sensor as if added to hass: it has hass and an entity id."""
    sensor = bs.LocalControlProblemSensor(c, NAME)
    sensor.hass = hass
    sensor.entity_id = "binary_sensor.ezhi_local_control_problem"
    sensor.async_write_ha_state = MagicMock()        # no entity platform on a bare core
    entries: list = []
    hass.bus.async_listen("logbook_entry", lambda event: entries.append(event.data))
    return sensor, entries


def test_the_problem_sensor_writes_its_reason_into_the_logbook():
    """The logbook shows "Problem" and has no place for attributes: the reason
    goes in as an entry of its own, tied to the sensor."""
    async def go(hass):
        err = LocalControlReadError(
            "the smart meter did not answer read localLink within 12 s", ("meter",))
        c = lc_coordinator(hass, FakeControl([ON, state(sem=False, consistent=False), err, ON]))
        sensor, entries = logbook_sensor(hass, c)
        await c.async_refresh()
        sensor._handle_coordinator_update()
        await hass.async_block_till_done()
        assert entries == []                                  # all well: nothing to say

        await c.async_refresh()
        sensor._handle_coordinator_update()
        await hass.async_block_till_done()
        assert len(entries) == 1
        assert entries[0]["entity_id"] == "binary_sensor.ezhi_local_control_problem"
        assert entries[0]["name"] == f"{NAME} Local Control Problem"
        assert entries[0]["message"] == "only the inverter is in the group, the smart meter is not"

        sensor._handle_coordinator_update()                   # the same problem goes on
        await hass.async_block_till_done()
        assert len(entries) == 1

    run(go)


def test_the_logbook_entry_names_the_silent_device_and_follows_a_change_of_cause():
    async def go(hass):
        err = LocalControlReadError(
            "the smart meter did not answer read localLink within 12 s", ("meter",))
        c = lc_coordinator(hass, FakeControl([ON]))
        sensor, entries = logbook_sensor(hass, c)
        await c.async_refresh()
        c.last_update_success = False
        c.last_exception = err
        sensor._handle_coordinator_update()
        await hass.async_block_till_done()
        assert [e["message"] for e in entries] == ["the smart meter did not answer"]

        c.last_exception = LocalControlReadError("both silent", ("inverter", "meter"))
        sensor._handle_coordinator_update()
        await hass.async_block_till_done()
        assert [e["message"] for e in entries][-1] == "neither the inverter nor the smart meter answered"
        assert len(entries) == 2

    run(go)


def test_a_problem_that_comes_back_is_written_again_and_its_end_is_not():
    async def go(hass):
        broken = state(sem=False, consistent=False)
        c = lc_coordinator(hass, FakeControl([broken, ON, broken]))
        sensor, entries = logbook_sensor(hass, c)
        for _ in range(3):
            await c.async_refresh()
            sensor._handle_coordinator_update()
            await hass.async_block_till_done()
        assert len(entries) == 2                  # twice the problem; the OK in between is the state's own

    run(go)


def test_the_logbook_waits_for_an_entity_id():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl([state(sem=False, consistent=False)]))
        sensor = bs.LocalControlProblemSensor(c, NAME)       # not added: no hass, no entity id
        await c.async_refresh()
        sensor._log_to_logbook()                              # must not raise

    run(go)


def test_the_status_and_the_problem_sensor_are_both_diagnostic():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl([ON]))
        assert sens.LocalControlStatusSensor(c, NAME).entity_category == "diagnostic"
        assert bs.LocalControlProblemSensor(c, NAME).entity_category == "diagnostic"

    run(go)


def test_the_problem_sensor_remembers_a_problem_that_went_away():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl([state(ezhi=False, consistent=False), ON]))
        sensor = bs.LocalControlProblemSensor(c, NAME)
        await c.async_refresh()
        assert sensor.is_on is True
        await c.async_refresh()
        assert sensor.is_on is False
        attrs = sensor.extra_state_attributes
        assert "reason" not in attrs and "cause" not in attrs
        assert "only the smart meter is in the group" in attrs["last_problem"]
        assert attrs["last_problem_at"]

    run(go)


# --- the retired switch -----------------------------------------------------------------------

def test_the_old_smart_linking_entity_is_removed_from_the_registry():
    async def go(hass):
        await er.async_load(hass)
        registry = er.async_get(hass)
        old = registry.async_get_or_create(
            "switch", DOMAIN, f"apsystems_{NAME}_cloud_third_link")
        keep = registry.async_get_or_create(
            "switch", DOMAIN, f"apsystems_{NAME}_cloud_eco")
        _remove_smart_linking_entity(hass, make_entry(name=NAME))
        assert registry.async_get(old.entity_id) is None
        assert registry.async_get(keep.entity_id) is not None
        _remove_smart_linking_entity(hass, make_entry(name=NAME))   # idempotent
        _remove_smart_linking_entity(hass, make_entry())            # no name: nothing to do

    run(go)


# --- start and stop -----------------------------------------------------------------------------

class FakeBus:
    """A broker for the objects async_start builds: records who is subscribed."""

    def __init__(self):
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.fail_on: str | None = None

    async def subscribe(self, topic, handler):
        if self.fail_on and self.fail_on in topic:
            raise OSError("subscribe refused")
        self.subscribed.append(topic)

        def unsubscribe():
            self.unsubscribed.append(topic)

        return unsubscribe

    async def publish(self, topic, payload):
        pass


class FakeSemApi:
    def __init__(self, bus):
        self._bus = bus
        self.subscribed = False

    async def async_subscribe(self):
        self.subscribed = True
        await self._bus.subscribe("sem-api", None)

    async def async_unsubscribe(self):
        self.subscribed = False
        self._bus.unsubscribed.append("sem-api")

    async def async_get_local_link(self):
        return {"status": "0", "config": {}}


class FakeInverterApi:
    async def async_get_config(self):
        return {"thirdLink": "0", "config": {}}

    async def async_get_raw(self, identifier):
        return {"isTcpNoDataCount": "5", "meterPower": "0"}


def install_fake_connect(monkeypatch, bus):
    from custom_components.apsystems_ezhi_local.ntp_responder import NtpResponder

    module = types.ModuleType("custom_components.apsystems_ezhi_local.mqtt_connect")
    module.make_sem_api = lambda hass, sem_id: FakeSemApi(bus)
    module.make_sem_feed = lambda hass, sem_id: SemFeed(sem_id, bus.subscribe)
    module.make_ntp_responder = lambda hass, targets, tz: NtpResponder(
        bus.publish, bus.subscribe, targets, tz)
    monkeypatch.setitem(sys.modules, module.__name__, module)


def lc_entry(**extra):
    return make_entry(**{
        CONF_CONTROL_TRANSPORT: TRANSPORT_LOCAL_MQTT,
        CONF_SEM_DEVICE_ID: SEM,
        **extra,
    })


def test_nothing_starts_unless_a_meter_or_the_time_answerer_is_configured():
    assert not rt.wants_local_control_runtime({})
    assert not rt.wants_local_control_runtime({CONF_SEM_DEVICE_ID: SEM})   # wrong transport
    assert rt.wants_local_control_runtime(lc_entry().data)
    assert rt.wants_local_control_runtime({CONF_ANSWER_NTP: True})


def test_a_meter_brings_up_the_feed_the_group_and_their_coordinators(monkeypatch):
    async def go(hass):
        bus = FakeBus()
        install_fake_connect(monkeypatch, bus)
        runtime = await rt.async_start(hass, lc_entry(), EZHI, FakeInverterApi())
        # setup did not wait for the devices: the first read runs in the background
        assert runtime.coordinator is not None and runtime.first_read is not None
        await runtime.first_read
        assert runtime.coordinator.last_update_success
        assert runtime.coordinator.data.active is False
        assert runtime.sem_coordinator is not None
        assert f"/event/SEM/{SEM}/post" in bus.subscribed
        assert runtime.ntp is None                       # off unless asked for
        await runtime.async_stop()
        assert f"/event/SEM/{SEM}/post" in bus.unsubscribed and "sem-api" in bus.unsubscribed

    run(go)


def test_the_time_answerer_covers_the_inverter_and_the_meter(monkeypatch):
    async def go(hass):
        bus = FakeBus()
        install_fake_connect(monkeypatch, bus)
        runtime = await rt.async_start(
            hass, lc_entry(**{CONF_ANSWER_NTP: True}), EZHI, FakeInverterApi())
        assert f"/ntp/EZHI/{EZHI}/get" in bus.subscribed
        assert f"/ntp/SEM/{SEM}/get" in bus.subscribed
        await runtime.async_stop()
        assert f"/ntp/EZHI/{EZHI}/get" in bus.unsubscribed

    run(go)


def test_the_time_answerer_works_without_a_meter(monkeypatch):
    async def go(hass):
        bus = FakeBus()
        install_fake_connect(monkeypatch, bus)
        entry = make_entry(**{CONF_CONTROL_TRANSPORT: TRANSPORT_LOCAL_MQTT, CONF_ANSWER_NTP: True})
        runtime = await rt.async_start(hass, entry, EZHI, FakeInverterApi())
        assert runtime.coordinator is None and runtime.sem_coordinator is None
        assert bus.subscribed == [f"/ntp/EZHI/{EZHI}/get"]
        await runtime.async_stop()

    run(go)


def test_a_failure_halfway_leaves_no_subscription_behind(monkeypatch):
    async def go(hass):
        bus = FakeBus()
        bus.fail_on = "/ntp/"
        install_fake_connect(monkeypatch, bus)
        with pytest.raises(OSError):
            await rt.async_start(
                hass, lc_entry(**{CONF_ANSWER_NTP: True}), EZHI, FakeInverterApi())
        # the meter's feed and API were up before the answerer failed -- all undone
        assert f"/event/SEM/{SEM}/post" in bus.unsubscribed
        assert "sem-api" in bus.unsubscribed

    run(go)


def test_a_stored_offset_that_is_no_longer_valid_falls_back_to_the_default():
    assert rt._stored_offset({CONF_LOCAL_CONTROL_OFFSET: 55}) == 55
    assert rt._stored_offset({CONF_LOCAL_CONTROL_OFFSET: 999}) == 30
    assert rt._stored_offset({CONF_LOCAL_CONTROL_OFFSET: "abc"}) == 30
    assert rt._stored_offset({}) == 30


# --- the actions ------------------------------------------------------------------------------

from custom_components.apsystems_ezhi_local import (  # noqa: E402
    _local_control_disable,
    _local_control_enable,
)


def service_hass(hass, coordinator):
    """A hass with one loaded entry, the way _resolve_entry_data looks for it."""
    entry = types.SimpleNamespace(entry_id="e1")
    hass.config_entries = MagicMock()
    hass.config_entries.async_entries = lambda domain: [entry]
    hass.data[DOMAIN] = {"e1": {LC_COORDINATOR: coordinator}}
    return hass


def call(**data):
    return types.SimpleNamespace(data=data)


def test_enable_uses_the_stored_offset_and_waits_when_asked():
    async def go(hass):
        control = FakeControl([ON])
        c = lc_coordinator(hass, control, offset=45)
        service_hass(hass, c)
        await _local_control_enable(hass, call(wait=True))
        assert control.enabled == [(45, False)] and control.waited == 1
        hass.config_entries.async_update_entry.assert_not_called()   # nothing new to remember

    run(go)


def test_enable_with_an_offset_remembers_it_once_the_devices_took_it():
    async def go(hass):
        control = FakeControl([ON])
        entry = make_entry(**{CONF_LOCAL_CONTROL_OFFSET: 30})
        c = lc_coordinator(hass, control, entry=entry)
        service_hass(hass, c)
        await _local_control_enable(hass, call(offset=80, wait=False))
        assert control.enabled == [(80, False)] and control.waited == 0
        assert c.offset == 80
        _a, kwargs = hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_LOCAL_CONTROL_OFFSET] == 80

    run(go)


def test_enable_refuses_an_offset_the_app_would_refuse():
    async def go(hass):
        control = FakeControl([ON])
        c = lc_coordinator(hass, control)
        service_hass(hass, c)
        with pytest.raises(HomeAssistantError, match="outside 0"):
            await _local_control_enable(hass, call(offset=500, wait=False))
        assert control.enabled == [] and c.offset == 30

    run(go)


def test_enable_reports_a_failure_and_does_not_remember_the_offset():
    async def go(hass):
        control = FakeControl([ON])
        control.fail = EzhiCloudError("the inverter reports no readings")
        c = lc_coordinator(hass, control)
        service_hass(hass, c)
        with pytest.raises(HomeAssistantError, match="no readings"):
            await _local_control_enable(hass, call(offset=80, wait=True))
        assert c.offset == 30
        # ...but it looked at once, and often: the command may have landed anyway
        assert c.update_interval.total_seconds() == rt.LC_FAST_POLL_S

    run(go)


def test_disable_dissolves_the_group():
    async def go(hass):
        control = FakeControl([ON])
        c = lc_coordinator(hass, control)
        service_hass(hass, c)
        await _local_control_disable(hass, call())
        assert control.disabled == 1

    run(go)


def test_the_actions_say_what_is_missing_when_local_control_is_not_set_up():
    async def go(hass):
        service_hass(hass, None)
        for fn in (_local_control_enable, _local_control_disable):
            with pytest.raises(HomeAssistantError, match="smart meter's id"):
                await fn(hass, call(wait=True))

    run(go)


# --- the options form -------------------------------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402

from custom_components.apsystems_ezhi_local import config_flow as cf  # noqa: E402


def options_flow(hass, **entry_data):
    entry = make_entry(**{"ip_address": "10.0.0.2", "name": NAME, **entry_data})
    hass.config_entries = MagicMock()
    hass.config_entries.async_get_known_entry = lambda _id: entry
    hass.config_entries.async_reload = AsyncMock()
    flow = cf.APsystemsEZHIOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "f1"
    flow._probe_local_mqtt = AsyncMock(return_value=None)
    flow._probe_sem = AsyncMock(return_value=None)
    return flow, entry


def form(**over):
    base = {"scan_interval_output": 5, "scan_interval_alarm": 60,
            "cloud_scan_interval": 60, CONF_CONTROL_TRANSPORT: TRANSPORT_LOCAL_MQTT}
    return {**base, **over}


def test_a_valid_meter_id_is_probed_and_saved():
    async def go(hass):
        flow, entry = options_flow(hass)
        result = await flow.async_step_device_options(
            form(**{CONF_SEM_DEVICE_ID: " M01234567890 ", CONF_ANSWER_NTP: True}))
        assert result["type"] == "create_entry"
        flow._probe_sem.assert_awaited_once_with("M01234567890")
        _a, kwargs = hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_SEM_DEVICE_ID] == "M01234567890"
        assert kwargs["data"][CONF_ANSWER_NTP] is True
        hass.config_entries.async_reload.assert_awaited_once()

    run(go)


def test_a_bad_meter_id_is_refused_before_anything_is_asked():
    async def go(hass):
        flow, entry = options_flow(hass)
        result = await flow.async_step_device_options(form(**{CONF_SEM_DEVICE_ID: "M033/1"}))
        assert result["errors"] == {"base": "invalid_sem_id"}
        flow._probe_local_mqtt.assert_not_awaited()
        flow._probe_sem.assert_not_awaited()
        hass.config_entries.async_update_entry.assert_not_called()

    run(go)


def test_a_meter_that_does_not_answer_is_not_saved():
    async def go(hass):
        flow, entry = options_flow(hass)
        flow._probe_sem = AsyncMock(return_value="sem_no_reply")
        result = await flow.async_step_device_options(form(**{CONF_SEM_DEVICE_ID: SEM}))
        assert result["errors"] == {"base": "sem_no_reply"}
        hass.config_entries.async_update_entry.assert_not_called()

    run(go)


def test_the_meter_is_only_asked_after_the_inverter_proved_the_broker():
    async def go(hass):
        flow, entry = options_flow(hass)
        flow._probe_local_mqtt = AsyncMock(return_value="mqtt_no_reply")
        result = await flow.async_step_device_options(form(**{CONF_SEM_DEVICE_ID: SEM}))
        assert result["errors"] == {"base": "mqtt_no_reply"}
        flow._probe_sem.assert_not_awaited()

    run(go)


def test_a_meter_id_or_the_time_answerer_without_local_mqtt_is_refused():
    async def go(hass):
        flow, entry = options_flow(hass)
        r1 = await flow.async_step_device_options(
            form(**{CONF_CONTROL_TRANSPORT: "cloud", CONF_SEM_DEVICE_ID: SEM}))
        assert r1["errors"] == {"base": "sem_needs_local_mqtt"}
        r2 = await flow.async_step_device_options(
            form(**{CONF_CONTROL_TRANSPORT: "cloud", CONF_ANSWER_NTP: True}))
        assert r2["errors"] == {"base": "ntp_needs_local_mqtt"}
        hass.config_entries.async_update_entry.assert_not_called()

    run(go)


def test_clearing_the_meter_id_removes_it_and_keeps_the_stored_offset():
    async def go(hass):
        flow, entry = options_flow(hass, **{
            CONF_SEM_DEVICE_ID: SEM, CONF_LOCAL_CONTROL_OFFSET: 55,
            CONF_CONTROL_TRANSPORT: TRANSPORT_LOCAL_MQTT})
        result = await flow.async_step_device_options(form())   # field left empty: absent
        assert result["type"] == "create_entry"
        flow._probe_sem.assert_not_awaited()
        _a, kwargs = hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_SEM_DEVICE_ID] == ""
        assert kwargs["data"][CONF_LOCAL_CONTROL_OFFSET] == 55

    run(go)


def test_the_meter_probe_reads_the_meters_link_and_always_unsubscribes(monkeypatch):
    async def go(hass):
        api = FakeSemApi(FakeBus())
        reads = []

        async def read():
            reads.append(1)
            return {"status": "0"}

        api.async_get_local_link = read
        module = types.ModuleType("custom_components.apsystems_ezhi_local.mqtt_connect")
        module.make_sem_api = lambda h, sem_id: api
        monkeypatch.setitem(sys.modules, module.__name__, module)

        flow = cf.APsystemsEZHIOptionsFlow()
        flow.hass = hass
        assert await flow._probe_sem(SEM) is None and reads == [1]
        assert api._bus.unsubscribed == ["sem-api"]

        async def silent():
            raise EzhiCloudError("no answer")

        api.async_get_local_link = silent
        assert await flow._probe_sem(SEM) == "sem_no_reply"
        assert api._bus.unsubscribed == ["sem-api", "sem-api"]

    run(go)


# --- the setup form ---------------------------------------------------------------------------------

import json  # noqa: E402

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "apsystems_ezhi_local"
INVERTER_ID = "D01234567890"


def setup_flow(hass, monkeypatch, *, device_id=INVERTER_ID, unreachable=False):
    class FakeApi:
        def __init__(self, ip_address, *args, **kwargs):
            self.ip_address = ip_address

        async def get_device_info(self):
            if unreachable:
                raise TimeoutError()
            return types.SimpleNamespace(deviceId=device_id)

    monkeypatch.setattr(cf, "APsystemsEZHI", FakeApi)
    monkeypatch.setattr(cf, "async_get_clientsession", lambda _hass: object())
    flow = cf.APsystemsEZHILocalAPIFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "setup1"
    flow.context = {"source": "user"}
    flow._probe_local_mqtt = AsyncMock(return_value=None)
    flow._probe_sem = AsyncMock(return_value=None)
    return flow


def setup_form(**over):
    base = {"ip_address": "10.0.0.2", "name": NAME, "check": True,
            "scan_interval_output": 5, "scan_interval_alarm": 60,
            "cloud_scan_interval": 60, CONF_CONTROL_TRANSPORT: "cloud"}
    return {**base, **over}


def field_names(schema):
    return {str(key) for key in schema.schema}


def suggested(schema, name):
    return next(key.description["suggested_value"]
                for key in schema.schema if str(key) == name)


def test_the_setup_form_has_every_field_the_options_form_has(monkeypatch):
    """The meter, the transport and the time answers used to be reachable only
    through Configure, after the integration had been set up without them."""
    async def go(hass):
        flow = setup_flow(hass, monkeypatch)
        setup = field_names((await flow.async_step_user())["data_schema"])
        opts, _entry = options_flow(hass)
        options = field_names(opts._device_options_schema())
        assert options <= setup, options - setup
        assert {CONF_SEM_DEVICE_ID, CONF_ANSWER_NTP, CONF_CONTROL_TRANSPORT} <= setup

    run(go)


def test_a_plain_setup_saves_what_the_options_form_would_have_defaulted(monkeypatch):
    async def go(hass):
        flow = setup_flow(hass, monkeypatch)
        result = await flow.async_step_user(setup_form())
        assert result["type"] == "create_entry" and result["title"] == NAME
        data = result["data"]
        assert data[CONF_CONTROL_TRANSPORT] == "cloud"
        assert data[CONF_SEM_DEVICE_ID] == "" and data[CONF_ANSWER_NTP] is False
        assert data["cloud_refresh_token"] == "" and data["ip_address"] == "10.0.0.2"
        flow._probe_local_mqtt.assert_not_awaited()      # nothing asked for, nothing probed
        flow._probe_sem.assert_not_awaited()

    run(go)


def test_the_meter_can_be_set_up_from_the_start(monkeypatch):
    async def go(hass):
        flow = setup_flow(hass, monkeypatch)
        result = await flow.async_step_user(setup_form(**{
            CONF_CONTROL_TRANSPORT: TRANSPORT_LOCAL_MQTT,
            CONF_SEM_DEVICE_ID: " M01234567890 ", CONF_ANSWER_NTP: True}))
        assert result["type"] == "create_entry"
        # The inverter's id was read while the connection was checked, and the
        # probe addresses the inverter by it.
        flow._probe_local_mqtt.assert_awaited_once_with(INVERTER_ID)
        flow._probe_sem.assert_awaited_once_with("M01234567890")
        data = result["data"]
        assert data[CONF_SEM_DEVICE_ID] == "M01234567890"
        assert data[CONF_CONTROL_TRANSPORT] == TRANSPORT_LOCAL_MQTT
        assert data[CONF_ANSWER_NTP] is True

    run(go)


def test_a_meter_without_the_local_transport_is_refused_and_the_form_keeps_the_input(monkeypatch):
    async def go(hass):
        flow = setup_flow(hass, monkeypatch)
        result = await flow.async_step_user(setup_form(**{CONF_SEM_DEVICE_ID: SEM, "name": "Mine"}))
        assert result["type"] == "form"
        assert result["errors"] == {"base": "sem_needs_local_mqtt"}
        assert suggested(result["data_schema"], CONF_SEM_DEVICE_ID) == SEM
        assert suggested(result["data_schema"], "name") == "Mine"      # not retyped

    run(go)


def test_a_meter_that_does_not_answer_is_not_set_up(monkeypatch):
    async def go(hass):
        flow = setup_flow(hass, monkeypatch)
        flow._probe_sem = AsyncMock(return_value="sem_no_reply")
        result = await flow.async_step_user(setup_form(**{
            CONF_CONTROL_TRANSPORT: TRANSPORT_LOCAL_MQTT, CONF_SEM_DEVICE_ID: SEM}))
        assert result["type"] == "form" and result["errors"] == {"base": "sem_no_reply"}

    run(go)


def test_an_unreachable_inverter_stops_the_setup_before_anything_else(monkeypatch):
    async def go(hass):
        flow = setup_flow(hass, monkeypatch, unreachable=True)
        result = await flow.async_step_user(setup_form(**{
            CONF_CONTROL_TRANSPORT: TRANSPORT_LOCAL_MQTT}))
        assert result["errors"] == {"base": "connection_refused"}
        flow._probe_local_mqtt.assert_not_awaited()

    run(go)


def test_without_the_connection_check_the_probe_is_left_to_find_the_id(monkeypatch):
    async def go(hass):
        flow = setup_flow(hass, monkeypatch)
        await flow.async_step_user(setup_form(check=False, **{
            CONF_CONTROL_TRANSPORT: TRANSPORT_LOCAL_MQTT}))
        flow._probe_local_mqtt.assert_awaited_once_with("")

    run(go)


def test_a_half_filled_account_is_refused_at_setup_too(monkeypatch):
    async def go(hass):
        flow = setup_flow(hass, monkeypatch)
        result = await flow.async_step_user(setup_form(cloud_username="me"))
        assert result["errors"] == {"base": "incomplete_credentials"}

    run(go)


# --- every field and every error can be read ------------------------------------------------------------

def translation(name):
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


TRANSLATIONS = ("strings.json", "translations/en.json", "translations/de.json")


def test_every_field_of_both_forms_has_a_label_in_every_language(monkeypatch):
    """A field with no label shows its raw key (`sem_device_id`) in the dialog."""
    async def go(hass):
        flow = setup_flow(hass, monkeypatch)
        setup = field_names((await flow.async_step_user())["data_schema"])
        opts, _entry = options_flow(hass)
        options = field_names(opts._device_options_schema())
        for name in TRANSLATIONS:
            data = translation(name)
            labels = data["config"]["step"]["user"]["data"]
            assert not {k for k in setup if k not in labels}, (name, "setup")
            labels = data["options"]["step"]["device_options"]["data"]
            assert not {k for k in options if k not in labels}, (name, "options")
            for label in labels.values():
                assert label.strip() and label != label.lower().replace(" ", "_")

    run(go)


def test_the_new_fields_explain_themselves_in_every_language():
    for name in TRANSLATIONS:
        data = translation(name)
        for section in (data["config"]["step"]["user"], data["options"]["step"]["device_options"]):
            for key in (CONF_SEM_DEVICE_ID, CONF_ANSWER_NTP, CONF_CONTROL_TRANSPORT):
                assert section["data_description"][key].strip(), (name, key)


def test_every_error_either_form_can_give_has_a_text_in_every_language():
    setup_errors = {
        "connection_refused", "invalid_auth", "cannot_connect", "incomplete_credentials",
        "invalid_sem_id", "sem_needs_local_mqtt", "ntp_needs_local_mqtt",
        "mqtt_not_configured", "mqtt_device_unknown", "mqtt_no_reply", "sem_no_reply",
    }
    options_errors = (setup_errors - {"connection_refused"}) | {"group_still_active"}
    for name in TRANSLATIONS:
        data = translation(name)
        assert not {k for k in setup_errors if not data["config"]["error"].get(k, "").strip()}, name
        assert not {k for k in options_errors if not data["options"]["error"].get(k, "").strip()}, name


def test_the_error_keys_in_the_code_are_all_covered_above():
    """So a new error key cannot be added to the flow and forgotten here."""
    import re

    source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    used = set(re.findall(r'(?:errors\["base"\]\s*=|return)\s*"([a-z_]+)"', source))
    known = {
        "connection_refused", "invalid_auth", "cannot_connect", "incomplete_credentials",
        "invalid_sem_id", "sem_needs_local_mqtt", "ntp_needs_local_mqtt", "mqtt_not_configured",
        "mqtt_device_unknown", "mqtt_no_reply", "sem_no_reply", "group_still_active",
        "cloud_auth_failed", "missing_credentials",
    }
    assert used <= known, used - known


# --- the whole entry: setup and unload ----------------------------------------------------------

from custom_components.apsystems_ezhi_local import (  # noqa: E402
    PLATFORMS,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.apsystems_ezhi_local.const import (  # noqa: E402
    LOCAL_CONTROL,
    MQTT_TRANSPORT,
    SEM_COORDINATOR,
)


class FakeLocalHttpApi:
    """The inverter's local HTTP API, which setup also touches."""

    def __init__(self, *args, **kwargs):
        pass

    async def get_device_info(self):
        return types.SimpleNamespace(deviceId=EZHI)

    async def get_alarm(self):
        return None

    async def get_output_data(self):
        return None


class FakeMqttInverter(FakeInverterApi):
    def __init__(self):
        self.subscribed = False
        self.unsubscribed = False

    async def async_subscribe(self):
        self.subscribed = True

    async def async_unsubscribe(self):
        self.unsubscribed = True

    async def async_poll_all(self):
        return {"config": {"systemMode": "1", "thirdLink": "0"}, "output": {},
                "device": {}, "extras": {}}


def setup_environment(monkeypatch, hass, bus, inverter):
    import homeassistant.components as components

    import custom_components.apsystems_ezhi_local as comp

    monkeypatch.setattr(comp, "APsystemsEZHI", FakeLocalHttpApi)
    monkeypatch.setattr(comp, "async_get_clientsession", lambda _hass: object())

    fake_mqtt = types.ModuleType("homeassistant.components.mqtt")

    async def ready(_hass):
        return True

    fake_mqtt.async_wait_for_mqtt_client = ready
    monkeypatch.setitem(sys.modules, "homeassistant.components.mqtt", fake_mqtt)
    monkeypatch.setattr(components, "mqtt", fake_mqtt, raising=False)

    install_fake_connect(monkeypatch, bus)
    sys.modules["custom_components.apsystems_ezhi_local.mqtt_connect"].make_mqtt_api = (
        lambda _hass, _device_id: inverter)

    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_entries = lambda domain: [entry_holder["entry"]]


entry_holder: dict = {}


def full_entry(**extra):
    entry = make_entry(**{
        "ip_address": "10.0.0.2", "name": NAME,
        CONF_CONTROL_TRANSPORT: TRANSPORT_LOCAL_MQTT,
        **extra,
    })
    entry_holder["entry"] = entry
    return entry


def test_setup_brings_up_local_control_and_unload_takes_it_down_again(monkeypatch):
    async def go(hass):
        await er.async_load(hass)
        bus, inverter = FakeBus(), FakeMqttInverter()
        setup_environment(monkeypatch, hass, bus, inverter)
        entry = full_entry(**{CONF_SEM_DEVICE_ID: SEM, CONF_ANSWER_NTP: True})

        assert await async_setup_entry(hass, entry) is True
        data = hass.data[DOMAIN][entry.entry_id]
        runtime = data[LOCAL_CONTROL]
        assert runtime is not None
        assert data[LC_COORDINATOR] is runtime.coordinator
        assert data[SEM_COORDINATOR] is runtime.sem_coordinator
        assert data[MQTT_TRANSPORT] is inverter and inverter.subscribed
        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(entry, PLATFORMS)
        for service in ("local_control_enable", "local_control_disable"):
            assert hass.services.has_service(DOMAIN, service)

        await runtime.first_read
        assert runtime.coordinator.data is not None
        assert f"/event/SEM/{SEM}/post" in bus.subscribed
        assert f"/ntp/EZHI/{EZHI}/get" in bus.subscribed

        assert await async_unload_entry(hass, entry) is True
        assert f"/event/SEM/{SEM}/post" in bus.unsubscribed
        assert f"/ntp/SEM/{SEM}/get" in bus.unsubscribed
        assert inverter.unsubscribed
        assert DOMAIN not in hass.data or not hass.data[DOMAIN]
        for service in ("local_control_enable", "local_control_disable"):
            assert not hass.services.has_service(DOMAIN, service)

    run(go)


def test_an_entry_without_a_meter_sets_up_exactly_as_before(monkeypatch):
    async def go(hass):
        await er.async_load(hass)
        bus, inverter = FakeBus(), FakeMqttInverter()
        setup_environment(monkeypatch, hass, bus, inverter)
        entry = full_entry()
        assert await async_setup_entry(hass, entry) is True
        data = hass.data[DOMAIN][entry.entry_id]
        assert data[LOCAL_CONTROL] is None
        assert data[LC_COORDINATOR] is None and data[SEM_COORDINATOR] is None
        assert bus.subscribed == []
        assert await async_unload_entry(hass, entry) is True

    run(go)


def test_a_meter_that_cannot_be_subscribed_does_not_take_the_entry_down(monkeypatch):
    async def go(hass):
        await er.async_load(hass)
        bus, inverter = FakeBus(), FakeMqttInverter()
        bus.fail_on = "/event/SEM/"
        setup_environment(monkeypatch, hass, bus, inverter)
        entry = full_entry(**{CONF_SEM_DEVICE_ID: SEM})
        assert await async_setup_entry(hass, entry) is True
        data = hass.data[DOMAIN][entry.entry_id]
        assert data[LOCAL_CONTROL] is None and data[LC_COORDINATOR] is None
        assert data[MQTT_TRANSPORT] is inverter                # the inverter's side is unaffected
        assert "sem-api" in bus.unsubscribed                    # and nothing was left subscribed
        assert await async_unload_entry(hass, entry) is True

    run(go)


# --- availability, settling, expiry ----------------------------------------------------------------

def test_the_switch_stays_on_offer_while_a_read_fails_so_the_group_can_still_be_dissolved():
    """A meter that lost power fails every read. The switch is the one way in the
    UI to dissolve the group, and it must not go away with the meter."""
    async def go(hass):
        c, switch = make_switch(hass, FakeControl())
        c.data = ON
        c.last_update_success = False
        assert switch.available is True
        assert switch.extra_state_attributes["last_read_failed"] is True
        c.data = None                          # nothing ever read: nothing to show
        assert switch.available is False

    run(go)


def test_the_asked_for_state_is_written_again_when_it_runs_out(monkeypatch):
    """With always_update off, a group that never forms reads the same every
    time and wakes nobody -- the switch would stay 'on' on screen forever."""
    async def go(hass):
        clock = Clock()
        monkeypatch.setattr(sw, "time", types.SimpleNamespace(monotonic=clock))
        scheduled = []
        monkeypatch.setattr(
            sw, "async_call_later",
            lambda _hass, delay, action: scheduled.append((delay, action)) or (lambda: None))
        c, switch = make_switch(hass, FakeControl([OFF]))
        writes = []
        switch.async_write_ha_state = lambda: writes.append(switch.is_on)
        await switch.async_turn_on()
        assert writes == [True]
        (delay, action), = scheduled
        assert delay > switch._PENDING_FOR_S
        clock.now += delay
        action(None)
        assert writes == [True, False]            # written again, and back to what the devices say

    run(go)


def test_the_problem_sensor_is_on_when_the_devices_cannot_be_read():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl([EzhiCloudError("timeout")]))
        sensor = bs.LocalControlProblemSensor(c, NAME)
        assert sensor.available is True
        assert sensor.is_on is None                      # nothing read yet: no verdict
        for _ in range(rt.LC_READ_FAILURES_TOLERATED):
            await c.async_refresh()
            assert sensor.is_on is None                  # the first reads after a start: still no verdict
        await c.async_refresh()
        assert not c.last_update_success
        assert sensor.available is True and sensor.is_on is True
        assert "did not answer" in sensor.extra_state_attributes["reason"]

    run(go)


def test_the_problem_sensor_waits_for_the_group_to_settle():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl())
        sensor = bs.LocalControlProblemSensor(c, NAME)
        c.data = state(no_data=20)                       # active, no readings yet
        assert sensor.is_on is True
        c.speed_up()                                     # a command was just given
        assert sensor.is_on is False
        c.data = state(sem=False, consistent=False)      # half a group is a problem regardless
        assert sensor.is_on is True

    run(go)


# --- the options form must not strand a group ------------------------------------------------------

def test_the_meter_id_cannot_be_removed_while_the_group_stands():
    async def go(hass):
        flow, entry = options_flow(hass, **{CONF_SEM_DEVICE_ID: SEM})
        hass.data[DOMAIN] = {entry.entry_id: {
            LC_COORDINATOR: types.SimpleNamespace(data=ON, control=FakeControl())}}
        for other in ({}, {CONF_SEM_DEVICE_ID: "M99999999999"}):
            result = await flow.async_step_device_options(form(**other))
            assert result["errors"] == {"base": "group_still_active"}
        hass.config_entries.async_update_entry.assert_not_called()
        # the same id again is no change at all
        result = await flow.async_step_device_options(form(**{CONF_SEM_DEVICE_ID: SEM}))
        assert result["type"] == "create_entry"

    run(go)


def test_the_meter_id_can_be_removed_once_the_group_is_gone():
    async def go(hass):
        flow, entry = options_flow(hass, **{CONF_SEM_DEVICE_ID: SEM})
        hass.data[DOMAIN] = {entry.entry_id: {
            LC_COORDINATOR: types.SimpleNamespace(data=OFF, control=FakeControl())}}
        result = await flow.async_step_device_options(form())
        assert result["type"] == "create_entry"

    run(go)


# --- diagnostics -----------------------------------------------------------------------------------------

def test_the_diagnostics_section_for_local_control_is_none_when_it_is_not_set_up():
    from custom_components.apsystems_ezhi_local.diagnostics import _local_control_section

    assert _local_control_section({}) is None
    section = _local_control_section({
        LC_COORDINATOR: types.SimpleNamespace(last_update_success=True, data=ON),
        "SEM_COORDINATOR": types.SimpleNamespace(last_update_success=True, data={"p": 5.0}),
    })
    assert section["group"]["data"]["ezhi_member"] is True
    assert section["group"]["problem"] is None and section["group"]["last_problem"] is None
    assert section["meter"]["data"] == {"p": 5.0}


# --- the on-grid power is refused outside Local mode -------------------------------------------------------

class FakeModeCoordinator:
    """The control coordinator, as far as the setpoint guard looks at it."""

    def __init__(self, mode, after_refresh=None, refresh_hangs=False):
        self.data = {"config": {"systemMode": mode}}
        self._after = after_refresh
        self._hangs = refresh_hangs
        self.refreshes = 0

    async def async_refresh(self):
        self.refreshes += 1
        if self._hangs:
            await asyncio.sleep(3600)
        if self._after is not None:
            self.data = {"config": {"systemMode": self._after}}


class FakeSetpointApi:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent: list[int] = []

    async def set_power(self, power):
        self.sent.append(power)
        return self.ok

    async def get_power(self):
        return 0


def setpoint_entry(coordinator, api=None, lc_data=None):
    from custom_components.apsystems_ezhi_local.const import CLOUD_COORDINATOR

    data = {"COORDINATOR": types.SimpleNamespace(api=api or FakeSetpointApi())}
    if coordinator is not None:
        data[CLOUD_COORDINATOR] = coordinator
    if lc_data is not None:
        data[LC_COORDINATOR] = types.SimpleNamespace(data=lc_data, control=FakeControl())
    return data


def test_the_setpoint_is_refused_in_a_mode_that_ignores_it():
    async def go(hass):
        from custom_components.apsystems_ezhi_local import async_write_setpoint

        api = FakeSetpointApi()
        entry_data = setpoint_entry(FakeModeCoordinator("1"), api)      # Balcony Storage
        with pytest.raises(HomeAssistantError) as err:
            await async_write_setpoint(entry_data, 300)
        assert "Balcony Storage" in str(err.value) and "Local" in str(err.value)
        assert "nothing was sent" in str(err.value)
        assert "try again" not in str(err.value)             # the inverter itself said so
        assert api.sent == []                       # not a warning: it never went out

    run(go)


def test_the_setpoint_is_written_in_local_mode_without_asking_again():
    async def go(hass):
        from custom_components.apsystems_ezhi_local import async_write_setpoint

        api = FakeSetpointApi()
        coordinator = FakeModeCoordinator("4")
        await async_write_setpoint(setpoint_entry(coordinator, api), 300)
        assert api.sent == [300] and coordinator.refreshes == 0

    run(go)


def test_a_stale_mode_does_not_block_someone_who_just_switched_to_local():
    """The coordinator still says Balcony Storage, the inverter already says
    Local: the refusal is made on the inverter's answer, not on the old one."""
    async def go(hass):
        from custom_components.apsystems_ezhi_local import async_write_setpoint

        api = FakeSetpointApi()
        coordinator = FakeModeCoordinator("1", after_refresh="4")
        await async_write_setpoint(setpoint_entry(coordinator, api), -200)
        assert api.sent == [-200] and coordinator.refreshes == 1

    run(go)


def test_when_the_inverter_cannot_be_asked_the_last_known_mode_decides(monkeypatch):
    async def go(hass):
        from custom_components.apsystems_ezhi_local import async_write_setpoint, entity

        monkeypatch.setattr(entity, "MODE_CHECK_TIMEOUT_S", 0.01)
        api = FakeSetpointApi()
        entry_data = setpoint_entry(FakeModeCoordinator("2", refresh_hangs=True), api)
        with pytest.raises(HomeAssistantError, match="Portable") as err:
            await async_write_setpoint(entry_data, 100)
        assert api.sent == []
        assert "try again in a moment" in str(err.value)     # it is not a fresh reading

    run(go)


def test_an_unknown_mode_is_never_a_reason_to_refuse():
    async def go(hass):
        from custom_components.apsystems_ezhi_local import async_write_setpoint

        # No control layer at all.
        api = FakeSetpointApi()
        await async_write_setpoint(setpoint_entry(None, api), 100)
        # A control layer that has not read the mode yet.
        coordinator = FakeModeCoordinator("4")
        coordinator.data = None
        await async_write_setpoint(setpoint_entry(coordinator, api), 200)
        assert api.sent == [100, 200]

    run(go)


def test_the_refusal_points_at_the_offset_while_local_control_holds_the_inverter():
    async def go(hass):
        from custom_components.apsystems_ezhi_local import async_write_setpoint

        entry_data = setpoint_entry(FakeModeCoordinator("1"), lc_data=ON)
        with pytest.raises(HomeAssistantError) as err:
            await async_write_setpoint(entry_data, 100)
        assert "Local Control Offset" in str(err.value)

    run(go)


def test_the_number_entity_refuses_too_and_leaves_the_inverter_alone():
    async def go(hass):
        api = FakeSetpointApi()
        entry_data = setpoint_entry(FakeModeCoordinator("1"), api)
        number = num.PowerLimit(api, device_name=NAME, sensor_name="On-Grid Power",
                                sensor_id="max_output_power", entry_data=entry_data)
        with pytest.raises(HomeAssistantError, match="Balcony Storage"):
            await number.async_set_native_value(300)
        assert api.sent == []
        entry_data["CLOUD_COORDINATOR"] = FakeModeCoordinator("4")
        await number.async_set_native_value(300)
        assert api.sent == [300]

    run(go)


def test_the_service_clamps_before_it_writes():
    async def go(hass):
        from custom_components.apsystems_ezhi_local import async_write_setpoint

        api = FakeSetpointApi()
        await async_write_setpoint(setpoint_entry(FakeModeCoordinator("4"), api), 5000)
        assert api.sent == [1200]
        api.ok = False
        with pytest.raises(HomeAssistantError, match="rejected the setpoint"):
            await async_write_setpoint(setpoint_entry(FakeModeCoordinator("4"), api), 100)

    run(go)


def test_the_diagnostics_carry_the_problem_text():
    from custom_components.apsystems_ezhi_local.diagnostics import _local_control_section
    from custom_components.apsystems_ezhi_local.local_control import problem_of

    broken = state(sem=False, consistent=False)
    section = _local_control_section({
        LC_COORDINATOR: types.SimpleNamespace(
            last_update_success=True, data=broken, problem=problem_of(broken),
            last_problem=problem_of(broken)),
    })
    assert "only the inverter is in the group" in section["group"]["problem"]
    assert section["group"]["last_problem"] == section["group"]["problem"]


# --- the status sensor: the reason in one word --------------------------------------------

def test_the_status_sensor_names_the_state_and_is_never_unavailable():
    async def go(hass):
        c = lc_coordinator(hass, FakeControl([ON]))
        sensor = sens.LocalControlStatusSensor(c, NAME)
        assert sensor.available is True and sensor.native_value is None   # nothing read yet
        assert sensor.device_class == "enum"
        assert sensor.options == list(STATUS_OPTIONS)
        assert sensor.translation_key == "local_control_status"
        assert sensor.unique_id == f"apsystems_{NAME}_local_control_status"
        await c.async_refresh()
        assert sensor.native_value == "regulating" and sensor.extra_state_attributes == {}
        c.data = OFF
        assert sensor.native_value == "off"
        c.data = state(ezhi=False, sem=True, consistent=False)
        assert sensor.native_value == "meter_only"
        attrs = sensor.extra_state_attributes
        assert attrs["cause"] == "meter_only" and "only the smart meter" in attrs["reason"]

    run(go)


def test_the_status_says_which_device_did_not_answer():
    async def go(hass):
        err = LocalControlReadError(
            "the smart meter did not answer read localLink within 12 s", ("meter",))
        c = lc_coordinator(hass, FakeControl([err]))
        sensor = sens.LocalControlStatusSensor(c, NAME)
        problem_sensor = bs.LocalControlProblemSensor(c, NAME)
        for _ in range(rt.LC_READ_FAILURES_TOLERATED + 1):
            await c.async_refresh()
        assert sensor.available is True
        assert sensor.native_value == "meter_silent"
        attrs = sensor.extra_state_attributes
        assert attrs["cause"] == "unreadable" and attrs["silent_devices"] == ["meter"]
        assert attrs["failed_reads_in_a_row"] == rt.LC_READ_FAILURES_TOLERATED + 1
        assert "the smart meter did not answer" in attrs["reason"]
        # The binary sensor carries the same facts for automations.
        assert problem_sensor.extra_state_attributes["silent_devices"] == ["meter"]

    run(go)


def test_the_status_keeps_the_last_problem_after_it_has_gone():
    async def go(hass):
        broken = state(sem=False, consistent=False)
        c = lc_coordinator(hass, FakeControl([broken, ON]))
        sensor = sens.LocalControlStatusSensor(c, NAME)
        await c.async_refresh()
        assert sensor.native_value == "inverter_only"
        await c.async_refresh()
        assert sensor.native_value == "regulating"
        assert "only the inverter" in sensor.extra_state_attributes["last_problem"]
        assert sensor.extra_state_attributes["last_problem_at"]

    run(go)


def test_every_status_the_sensor_can_show_has_a_text_in_every_language():
    for name in TRANSLATIONS:
        states = translation(name)["entity"]["sensor"]["local_control_status"]["state"]
        assert set(states) == set(STATUS_OPTIONS), name
        assert all(text.strip() for text in states.values()), name


# --- one missed answer must not blank the device ----------------------------------------------

class HttpApi:
    """Stands in for the local HTTP client: a script of replies."""

    def __init__(self, replies):
        self.replies = list(replies)

    async def get_output_data(self):
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, Exception):
            raise item
        return item


def test_a_missed_http_answer_keeps_the_last_values_for_a_while():
    async def go(hass):
        clock = Clock()
        reading = types.SimpleNamespace(pvP="120")
        c = ApSystemsDataCoordinator(hass, HttpApi([reading, TimeoutError()]))
        c._grace = Grace(HTTP_GRACE_S, clock)
        await c.async_refresh()
        assert c.last_update_success and c.data is reading
        clock.now += 10
        await c.async_refresh()
        assert c.last_update_success and c.data is reading       # kept, not unavailable
        clock.now += HTTP_GRACE_S
        await c.async_refresh()
        assert not c.last_update_success                         # a real outage still shows

    run(go)


def test_the_http_poll_recovers_when_the_inverter_answers_again():
    async def go(hass):
        clock = Clock()
        first, second = types.SimpleNamespace(pvP="1"), types.SimpleNamespace(pvP="2")
        c = ApSystemsDataCoordinator(hass, HttpApi([first, TimeoutError(), second]))
        c._grace = Grace(HTTP_GRACE_S, clock)
        for _ in range(3):
            await c.async_refresh()
            clock.now += 5
        assert c.last_update_success and c.data is second

    run(go)


def test_after_a_command_that_makes_the_inverter_reconnect_the_silence_is_covered_longer():
    async def go(hass):
        clock = Clock()
        reading = types.SimpleNamespace(pvP="120")
        c = ApSystemsDataCoordinator(hass, HttpApi([reading, TimeoutError()]))
        c._grace = Grace(HTTP_GRACE_S, clock)
        await c.async_refresh()
        c.extend_grace(RECONNECT_GRACE_S)
        clock.now += RECONNECT_GRACE_S - 1
        await c.async_refresh()
        assert c.last_update_success and c.data is reading
        clock.now += 2
        await c.async_refresh()
        assert not c.last_update_success

    run(go)


def control_api(replies):
    script = list(replies)

    async def async_get_config():
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        return item

    return types.SimpleNamespace(async_get_config=async_get_config)


def test_a_missed_control_poll_keeps_the_last_configuration_for_two_polls():
    async def go(hass):
        clock = Clock()
        mode = {"systemMode": "1"}
        c = ApSystemsCloudCoordinator(
            hass, make_entry(), control_api([mode, EzhiCloudError("timeout")]), 60)
        c._grace = Grace(CONTROL_GRACE_S, clock)
        await c.async_refresh()
        assert c.fresh and control_config(c.data) == mode
        clock.now += 60
        await c.async_refresh()
        assert c.last_update_success and control_config(c.data) == mode
        assert c.fresh is False                  # shown, but not a fresh reading
        clock.now += 60
        await c.async_refresh()
        assert c.last_update_success
        clock.now += 60
        await c.async_refresh()
        assert not c.last_update_success         # the third missed poll in a row

    run(go)


def test_the_acknowledged_mode_is_shown_until_a_poll_says_otherwise():
    async def go(hass):
        c = ApSystemsCloudCoordinator(
            hass, make_entry(), control_api([{"systemMode": "1", "socMin": "10"}]), 60)
        await c.async_refresh()
        c.apply_config({"systemMode": "4"})
        assert control_config(c.data) == {"systemMode": "4", "socMin": "10"}
        assert c.last_update_success
        await c.async_refresh()                  # the inverter says what it really is
        assert control_config(c.data)["systemMode"] == "1"

    run(go)


def test_a_mode_check_on_covered_data_does_not_count_as_a_fresh_reading():
    """async_require_local_mode refuses a write on the strength of the mode it
    read; data kept through a failed poll reads as a success to Home Assistant,
    so the refusal must still say that the inverter did not answer just now."""
    async def go(hass):
        mode = {"systemMode": "1"}
        c = ApSystemsCloudCoordinator(
            hass, make_entry(), control_api([mode, mode, EzhiCloudError("timeout")]), 60)
        await c.async_refresh()
        with pytest.raises(HomeAssistantError, match="Balcony Storage") as fresh:
            await async_require_local_mode({"CLOUD_COORDINATOR": c})
        assert "did not answer just now" not in str(fresh.value)
        with pytest.raises(HomeAssistantError, match="did not answer just now"):
            await async_require_local_mode({"CLOUD_COORDINATOR": c})
        assert c.last_update_success and c.fresh is False

    run(go)


def test_extend_grace_reaches_both_coordinators_of_the_entry():
    asked = []
    local = types.SimpleNamespace(extend_grace=lambda seconds: asked.append(("local", seconds)))
    control = types.SimpleNamespace(extend_grace=lambda seconds: asked.append(("control", seconds)))
    extend_grace({"COORDINATOR": local, "CLOUD_COORDINATOR": control})
    assert asked == [("local", RECONNECT_GRACE_S), ("control", RECONNECT_GRACE_S)]
    extend_grace({})                              # nothing configured: nothing to do
    extend_grace({"COORDINATOR": object()})


def test_forming_or_dissolving_the_group_extends_the_grace_of_the_other_coordinators():
    async def go(hass):
        asked = []
        peer = types.SimpleNamespace(extend_grace=asked.append)
        c = lc_coordinator(hass, FakeControl([ON]))
        c.peers = [peer, object()]
        c.speed_up()
        assert asked == [RECONNECT_GRACE_S]

    run(go)


# --- the On-Grid Power number on the inverter's HTTP side ------------------------------

class FakePowerApi:
    """Stands in for APsystemsEZHI: a script of get_power answers."""

    def __init__(self, *script):
        self.script = list(script)

    async def get_power(self):
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def power_number(api, entry_data=None):
    clock = Clock()
    number = num.PowerLimit(api, NAME, "On-Grid Power", "max_output_power",
                            entry_data=entry_data)
    number._grace = Grace(HTTP_GRACE_S, clock=clock)
    if entry_data is not None:                   # the grace under test is the one registered
        entry_data["GRACES"] = [number._grace]
    return number, clock


def test_one_missed_answer_does_not_make_the_on_grid_number_unavailable():
    async def go(hass):
        number, clock = power_number(FakePowerApi(300, TimeoutError("no answer")))
        await number.async_update()
        assert number._attr_available is True and number.state == 300
        clock.now += 15
        await number.async_update()                     # the miss
        assert number._attr_available is True and number.state == 300
        clock.now += HTTP_GRACE_S                       # silent for good: now it shows
        await number.async_update()
        assert number._attr_available is False

    run(go)


def test_the_on_grid_number_recovers_with_the_next_answer():
    async def go(hass):
        number, clock = power_number(
            FakePowerApi(300, TimeoutError("no answer"), 310))
        await number.async_update()
        clock.now += HTTP_GRACE_S + 1
        await number.async_update()
        assert number._attr_available is False
        await number.async_update()
        assert number._attr_available is True and number.state == 310

    run(go)


def test_the_on_grid_number_without_a_first_answer_is_unavailable():
    async def go(hass):
        number, _clock = power_number(FakePowerApi(TimeoutError("no answer")))
        await number.async_update()
        assert number._attr_available is False and number.state is None

    run(go)


def test_a_system_mode_change_extends_the_on_grid_numbers_grace_too():
    async def go(hass):
        entry_data: dict = {}
        number, clock = power_number(
            FakePowerApi(300, TimeoutError("no answer")), entry_data)
        await number.async_update()
        extend_grace(entry_data)                        # what the select does after a write
        clock.now += HTTP_GRACE_S + 60                  # past the plain grace, inside the extended one
        await number.async_update()
        assert number._attr_available is True and number.state == 300

    run(go)

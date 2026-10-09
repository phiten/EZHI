"""Switch platform for the APsystems EZHI integration.

On/off, backup power (EPS) and ECO are control-layer switches -- none of them
exists in the local HTTP API, which is the whole reason that layer was built.
The Local Control switch is the odd one out: it needs the local MQTT transport
and a smart meter, not the control coordinator.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant import config_entries
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import EzhiCloudError, control_config, is_running, wire_str
from .const import CLOUD_COORDINATOR, DOMAIN, LC_COORDINATOR
from homeassistant.helpers.event import async_call_later

from .entity import CLOUD_WRITE_TIMEOUT_S, EzhiCloudEntity, LocalControlEntity
from .lc_runtime import WRITE_TIMEOUT_S as LC_WRITE_TIMEOUT_S


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform."""
    config = hass.data[DOMAIN][config_entry.entry_id]

    # Local Control is independent of the cloud layer: it needs the broker and
    # the smart meter, nothing else.
    lc_coordinator = config.get(LC_COORDINATOR)
    if lc_coordinator is not None:
        add_entities([LocalControlSwitch(lc_coordinator, config[CONF_NAME])])

    cloud_coordinator = config.get(CLOUD_COORDINATOR)
    if cloud_coordinator is None:
        # No cloud credentials configured — local-only setup, nothing to add.
        return

    add_entities([
        EzhiCloudOnOffSwitch(cloud_coordinator, config[CONF_NAME]),
        EzhiCloudBackupPowerSwitch(cloud_coordinator, config[CONF_NAME]),
        EzhiCloudEcoSwitch(cloud_coordinator, config[CONF_NAME]),
    ])


class EzhiCloudOnOffSwitch(EzhiCloudEntity, SwitchEntity):
    """Turns the inverter on and off through the EMA cloud.

    Two things about this switch are not obvious:

    1. The wire format is inverted — the cloud field ``onOff`` reads "0" while
       the inverter is running.
    2. Switching it off is a one-way trip from Home Assistant. Once the inverter
       is down it drops off MQTT and the cloud can no longer reach it, so turning
       it back on needs PV/DC input or a 3 s press on the battery button.
    """

    _attr_icon = "mdi:power"
    # Not literally true -- we do poll real state -- but taken deliberately
    # for two practical reasons, not semantic purity:
    #   1. Renders as two separate buttons instead of one toggle. Turning
    #      this inverter off cannot be undone from HA, so it should take a
    #      deliberate press, not an accidental brush of a toggle.
    #   2. Removes a snap-back hazard. The cloud coordinator has
    #      always_update=False, so an unchanged post-write GET fires no
    #      listener and this entity gets no state write after the service
    #      call. A toggle's optimistic flip then has nothing confirming it,
    #      reverts, reads as "it didn't work" -- and risks a second write to
    #      a hybrid inverter. Do not "fix" this back to False.
    _attr_assumed_state = True

    def __init__(self, coordinator, device_name: str):
        super().__init__(coordinator, device_name, "on_off", "Inverter On")

    @property
    def is_on(self) -> bool | None:
        # is_running lives in cloud.py, beside async_set_on_off, so the one
        # piece of inverted-wire knowledge has a single home -- and one this
        # entity file cannot itself have a test for (no HA test harness here).
        return is_running(control_config(self.coordinator.data).get("onOff"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "remote_turn_on_limitation": (
                "Only works while the inverter is still online. Once off, wake it "
                "with PV/DC input or a 3 s press on the battery button."
            )
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        try:
            async with asyncio.timeout(CLOUD_WRITE_TIMEOUT_S):
                await self.coordinator.api.async_set_on_off(on)
        except TimeoutError as err:
            raise HomeAssistantError(
                f"the inverter did not answer within {CLOUD_WRITE_TIMEOUT_S} s "
                "-- the on/off command may or may not have been applied"
            ) from err
        except EzhiCloudError as err:
            raise HomeAssistantError(str(err)) from err
        # Re-read rather than trusting the write: confirm against the device.
        await self.coordinator.async_request_refresh()


class _EzhiCloudSystemModeSwitch(EzhiCloudEntity, SwitchEntity):
    """A boolean field inside the systemMode config blob.

    Not assumed_state, unlike the on/off switch above: these are reversible
    from Home Assistant, so a plain toggle is right and there is no reason to
    make them two deliberate buttons.
    """

    _key: str

    @property
    def is_on(self) -> bool | None:
        raw = control_config(self.coordinator.data).get(self._key)
        if raw is None:
            return None
        # Same normalisation the write path uses, so "1"/1/1.0/True all agree.
        return wire_str(raw) == "1"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(False)

    async def _async_write(self, on: bool) -> None:
        raise NotImplementedError

    async def _async_guarded(self, coro_factory, what: str) -> None:
        try:
            async with asyncio.timeout(CLOUD_WRITE_TIMEOUT_S):
                # The client re-reads the live config before posting, so this
                # is a GET plus a POST -- roughly double a single-call write.
                await coro_factory()
        except TimeoutError as err:
            raise HomeAssistantError(
                f"the inverter did not answer within {CLOUD_WRITE_TIMEOUT_S} s "
                f"-- the {what} change may or may not have been applied"
            ) from err
        except EzhiCloudError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()


class EzhiCloudBackupPowerSwitch(_EzhiCloudSystemModeSwitch):
    """EPS -- backup / emergency power on the off-grid side.

    This is the control the whole cloud layer was built for: it is not in the
    local API at all.

    Turning it on also clears ECO, because the firmware treats the two as
    mutually exclusive. That pairing lives in cloud.async_set_backup_power so
    it has one home and a test; this file has no HA test harness.

    The vendor app hides this switch in Local mode, but the write works there
    anyway -- measured 2026-08-01 from Home Assistant, EPS 1 -> 0 -> 1 while
    the device stayed in mode 4, no other field touched. A UI decision in the
    app, not an API restriction.
    """

    _attr_icon = "mdi:home-battery"
    _key = "EPS"

    def __init__(self, coordinator, device_name: str):
        super().__init__(coordinator, device_name, "eps", "Backup Power")

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {
            "mutually_exclusive_with": "ECO -- enabling backup power disables ECO",
            "vendor_app_visibility": (
                "The vendor app hides this switch in Local mode. Writing it from "
                "here works there regardless -- verified 2026-08-01."
            ),
        }

    async def _async_write(self, on: bool) -> None:
        await self._async_guarded(
            lambda: self.coordinator.api.async_set_backup_power(on), "backup power"
        )


class EzhiCloudEcoSwitch(_EzhiCloudSystemModeSwitch):
    """ECO -- shuts the off-grid side down after an hour with no load.

    Measured, so nobody re-derives it: ECO does NOT reduce standby draw. An
    A/B test found the same ~17 W either way. It only powers down the off-grid
    output when nothing is drawing from it.
    """

    _attr_icon = "mdi:leaf"
    _key = "ECO"

    def __init__(self, coordinator, device_name: str):
        super().__init__(coordinator, device_name, "eco", "ECO Mode")

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {
            "mutually_exclusive_with": "EPS -- enabling ECO disables backup power",
            "does_not_reduce_standby": (
                "Measured 2026-07-31: battery draw was ~17 W with and without ECO."
            ),
        }

    async def _async_write(self, on: bool) -> None:
        await self._async_guarded(
            lambda: self.coordinator.api.async_set_eco(on), "ECO mode"
        )


class LocalControlSwitch(LocalControlEntity, SwitchEntity):
    """Local Control: the inverter regulates to the smart meter by itself.

    On puts the meter and the inverter into one group (two commands, the meter
    first); off dissolves it (the inverter first). While the group stands the
    inverter follows the meter and holds the grid draw at the offset, with or
    without Home Assistant -- this switch only configures and reports.

    The state is read back from both devices every 30 s (every 5 s for a
    minute and a half after a change), so a group made or dissolved in the
    vendor app shows up here too.

    After a change the inverter reconnects (about 11 s) and starts regulating
    after about 28 s, and reads time out meanwhile. The switch therefore shows
    the state that was asked for until the devices confirm it (or 90 s have
    passed), instead of flipping back and forth.
    """

    _attr_icon = "mdi:link-variant"
    _PENDING_FOR_S = 90.0

    def __init__(self, coordinator, device_name: str):
        super().__init__(coordinator, device_name, "switch", "Local Control")
        self._pending: bool | None = None
        self._pending_until = 0.0
        self._expire_handle = None

    @property
    def available(self) -> bool:
        # The last known state stays on offer while a read fails. A meter that
        # lost power would otherwise take the switch with it -- and with the
        # switch, the one way in the UI to dissolve the group. The Problem
        # sensor, which does say that a read failed, is the place for the fault.
        return self.coordinator.data is not None

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_expiry()
        await super().async_will_remove_from_hass()

    def _cancel_expiry(self) -> None:
        if self._expire_handle is not None:
            self._expire_handle()
            self._expire_handle = None

    @property
    def _actual(self) -> bool | None:
        state = self.coordinator.data
        return None if state is None else state.active

    @property
    def is_on(self) -> bool | None:
        actual = self._actual
        if self._pending is not None:
            if actual == self._pending or time.monotonic() > self._pending_until:
                self._pending = None
            else:
                return self._pending
        return actual

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data
        attrs: dict[str, Any] = {
            "offset_to_apply": self.coordinator.offset,
            "last_read_failed": not self.coordinator.last_update_success,
        }
        if state is None:
            return attrs
        problem = self.coordinator.problem
        attrs.update({
            "offset_active": state.offset,
            "inverter_in_group": state.ezhi_member,
            "meter_in_group": state.sem_member,
            "configs_match": state.consistent,
            "meter_power_seen_by_inverter": state.meter_power,
            "seconds_without_meter_data": state.no_data_count,
            "problem": None if problem is None else problem.text,
        })
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        # No waiting for the regulation to start: the call returns once both
        # devices accepted their command, and the state follows by itself.
        await self._async_run(
            lambda: self.coordinator.control.async_enable(
                self.coordinator.offset, wait=False),
            want=True,
            what="set up the group",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_run(
            self.coordinator.control.async_disable,
            want=False,
            what="dissolve the group",
        )

    async def _async_run(self, command, *, want: bool, what: str) -> None:
        try:
            async with asyncio.timeout(LC_WRITE_TIMEOUT_S):
                await command()
        except TimeoutError as err:
            self.coordinator.speed_up()
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                f"the devices did not answer within {LC_WRITE_TIMEOUT_S} s -- "
                f"the attempt to {what} may or may not have been applied"
            ) from err
        except EzhiCloudError as err:
            raise HomeAssistantError(str(err)) from err
        self._pending = want
        self._pending_until = time.monotonic() + self._PENDING_FOR_S
        self._schedule_expiry()
        self.async_write_ha_state()
        self.coordinator.speed_up()
        await self.coordinator.async_request_refresh()

    def _schedule_expiry(self) -> None:
        """Write the state again when the asked-for state runs out.

        Nothing else would: the coordinator only wakes its entities when a read
        differs from the last one, and a group that never forms reads the same
        every time -- the switch would then stay "on" on screen indefinitely.
        """
        self._cancel_expiry()

        @callback
        def _expire(_now) -> None:
            self._expire_handle = None
            self.async_write_ha_state()

        self._expire_handle = async_call_later(
            self.hass, self._PENDING_FOR_S + 1, _expire)

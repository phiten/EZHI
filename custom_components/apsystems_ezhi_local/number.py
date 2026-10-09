"""Number platform for APsystems EZHI local API integration."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiohttp import client_exceptions

from homeassistant import config_entries
from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import CONF_NAME, PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CLOUD_COORDINATOR,
    CONF_LOCAL_CONTROL_OFFSET,
    DOMAIN,
    HTTP_GRACE_S,
    LC_COORDINATOR,
    LOGGER,
    MAX_VALUE,
    MIN_VALUE,
)
from .api import APsystemsEZHI
from .grace import Grace
from .cloud import EzhiCloudError, control_config
from .entity import (
    CLOUD_WRITE_TIMEOUT_S,
    LOCAL_CONTROL_HOLDS_MESSAGE,
    EzhiCloudEntity,
    LocalControlEntity,
    async_require_local_mode,
    local_control_holds_inverter,
)
from .lc_runtime import WRITE_TIMEOUT_S as LC_WRITE_TIMEOUT_S
from .local_control import LocalControlError, check_offset, max_offset


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: config_entries.ConfigEntry,
        add_entities: AddEntitiesCallback,
) -> None:
    """Set up the number platform."""
    config = hass.data[DOMAIN][config_entry.entry_id]
    # The coordinator's own client, not a second one: a second one carried the
    # default timeout of 10 s, which is also where Home Assistant starts to
    # complain that an update is slow, and it was blind to what the first had
    # on the wire (api.py counts the requests in flight).
    api = config["COORDINATOR"].api

    # update_before_add=True: PowerLimit is a plain, should_poll=True
    # NumberEntity and would otherwise sit at `unknown` until its first poll.
    # `config` rather than the coordinator itself: the cloud side is optional
    # and PowerLimit only reads the mode at write time, so it must not capture
    # a None that was true at setup.
    add_entities([
        PowerLimit(api, device_name=config[CONF_NAME], sensor_name="On-Grid Power",
                   sensor_id="max_output_power", entry_data=config),
    ], True)

    lc_coordinator = config.get(LC_COORDINATOR)
    if lc_coordinator is not None:
        add_entities([LocalControlOffsetNumber(lc_coordinator, config[CONF_NAME])])

    cloud_coordinator = config.get(CLOUD_COORDINATOR)
    if cloud_coordinator is not None:
        # No update_before_add here: these already have the coordinator's
        # data. update_before_add=True is not fire-and-forget -- entity_
        # platform awaits it, so it would put an undeadlined cloud GET
        # (cloud.py's no-deadline exemption is scoped to DataUpdateCoordinator
        # as the caller) on the setup path, on top of the refresh __init__.py
        # already did and deliberately capped at 20 s.
        add_entities([
            EzhiCloudSocNumber(cloud_coordinator, config[CONF_NAME], "socMin", "SOC Minimum"),
            EzhiCloudSocNumber(cloud_coordinator, config[CONF_NAME], "socMax", "SOC Maximum"),
            EzhiCloudSystemModeNumber(cloud_coordinator, config[CONF_NAME], SYSTEM_MODE_NUMBERS["userSetPower"], config),
            EzhiCloudSystemModeNumber(cloud_coordinator, config[CONF_NAME], SYSTEM_MODE_NUMBERS["dischargeProtection"], config),
        ])


class PowerLimit(NumberEntity):
    """Representation of a power limit control."""
    _attr_device_class = NumberDeviceClass.POWER
    _attr_available = False
    _attr_native_min_value = MIN_VALUE
    _attr_native_max_value = MAX_VALUE
    _attr_native_step = 10
    # See EzhiCloudEntity: the device name comes from device_info, so the
    # entity carries only its own half of the name.
    _attr_has_entity_name = True

    def __init__(self, api: APsystemsEZHI, device_name: str, sensor_name: str,
                 sensor_id: str, entry_data: dict | None = None):
        """Initialize the sensor."""
        self._api = api
        self._state = None
        self._device_name = device_name
        self._attr_name = sensor_name
        self._sensor_id = sensor_id
        self._entry_data = entry_data if entry_data is not None else {}
        # A missed answer keeps the last setpoint for a while instead of
        # turning the number unavailable (grace.py); a system mode change
        # extends it like it does for the coordinators (entity.extend_grace).
        self._grace = Grace(HTTP_GRACE_S)
        self._entry_data.setdefault("GRACES", []).append(self._grace)

    async def async_update(self):
        """Update the entity."""
        try:
            self._state = await self._api.get_power()
        except (TimeoutError, client_exceptions.ClientConnectionError) as err:
            if self._state is not None and self._grace.holds():
                LOGGER.debug("on-grid setpoint: %s; keeping %s W", err, self._state)
                return
            self._attr_available = False
            return
        self._grace.ok()
        self._attr_available = True

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unique_id(self) -> str | None:
        """Return the unique ID of the sensor."""
        return f"apsystems_{self._device_name}_{self._sensor_id}"

    async def async_set_native_value(self, value: float) -> None:
        """Set the value of the power limit."""
        # Refused outside Local mode: the inverter would answer SUCCESS and do
        # nothing. The check re-reads a mode that is not Local before it refuses,
        # so a switch to Local a moment ago is not held against the user.
        await async_require_local_mode(self._entry_data)
        try:
            if not await self._api.set_power(int(value)):
                LOGGER.error(
                    "the inverter rejected the on-grid setpoint %s W",
                    int(value))
            self._attr_available = True
        except (TimeoutError, client_exceptions.ClientConnectionError):
            self._attr_available = False
        await self.async_update()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={
                (DOMAIN, self._device_name)
            },
            name=self._device_name,
            manufacturer="APsystems",
            model="EZHI",
        )


_KEY_TO_KWARG = {"socMin": "soc_min", "socMax": "soc_max"}


def _safe_float(raw) -> float | None:
    """Parse a raw cloud value to float, tolerating both the missing case and
    a malformed one the same way. Before this, a raw of None returned a
    friendly None (-> entity state "unknown") but a non-numeric string raised
    a bare ValueError as a traceback -- inconsistent handling of two flavours
    of "this field isn't usable yet"."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class EzhiCloudSocNumber(EzhiCloudEntity, NumberEntity):
    """One of the two SOC bounds, written through the EMA cloud.

    The socLimit endpoint takes both bounds at once. Rather than pairing them
    up here from `coordinator.data` (which can be up to a poll interval old),
    only this entity's own bound is sent -- async_set_soc_limit re-reads the
    other one fresh. That keeps the untestable pairing logic out of this file
    (no HA harness here) and in cloud.py, where it is covered by
    tests/test_cloud.py.
    """

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, device_name: str, key: str, label: str):
        super().__init__(coordinator, device_name, key.lower(), label)
        self._key = key  # "socMin" or "socMax"

    @property
    def native_value(self) -> float | None:
        return _safe_float(control_config(self.coordinator.data).get(self._key))

    async def async_set_native_value(self, value: float) -> None:
        # round(), not int(): HA validates native_min/max_value but not
        # native_step, so a slider drag landing on e.g. 20.7 would otherwise
        # be silently truncated to 20 instead of rounded to 21.
        try:
            async with asyncio.timeout(CLOUD_WRITE_TIMEOUT_S):
                # async_set_soc_limit re-reads (a GET) before it posts, same
                # as async_set_system_mode -- unguarded worst case is roughly
                # double a single-call write.
                await self.coordinator.api.async_set_soc_limit(
                    **{_KEY_TO_KWARG[self._key]: round(value)}
                )
        except TimeoutError as err:
            raise HomeAssistantError(
                f"the inverter did not answer within {CLOUD_WRITE_TIMEOUT_S} s "
                "-- the SOC limit change may or may not have been applied"
            ) from err
        except EzhiCloudError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()


@dataclass(frozen=True)
class _SystemModeNumber:
    """Everything that differs between the systemMode-backed numbers."""

    key: str
    label: str
    unique_id: str
    minimum: float
    maximum: float
    step: float
    unit: str
    device_class: NumberDeviceClass
    icon: str
    note: str


# Ranges come from the vendor app's own bounds, not from guesswork:
#   userSetPower  -- the preset output power; app default 200 W, ceiling is
#                    powerLimit. Kept at the device ceiling rather than the
#                    live powerLimit so lowering powerLimit cannot strand the
#                    entity above its own max.
#
# powerLimit deliberately has NO entity here. It looks like a number and is
# not one: the vendor app only ever assigns it minPower (800) or maxPower
# (1200) from a "high power mode" toggle -- never a free value. That toggle
# shows a disclaimer that 1200 W "may cause the device output to exceed
# regulatory limits for grid connection", with the legal risk on the user, and
# on the way down it rewrites every schedule entry above 800 W. A 0-1200
# slider would have offered values the device never accepts, with no
# acknowledgement in front of the one that carries the disclaimer.
#
# It is the set_high_power_mode service instead, and the current value is
# readable as a sensor. An earlier version of this comment claimed the write
# went through remote/ezInverter/maxPower/{id} with a maxPowerFlag; that was
# wrong and is corrected in cloud.py next to HIGH_POWER_LIMIT -- that endpoint
# is a GET asking what ceiling the account may use, and powerLimit travels in
# the ordinary systemMode payload.
#   dischargeProtection -- percent, and cloud.py refuses anything under
#                    socMin + 2, the same rule the app enforces.
SYSTEM_MODE_NUMBERS = {
    "userSetPower": _SystemModeNumber(
        key="userSetPower", label="Preset Output Power", unique_id="user_set_power",
        minimum=0, maximum=1200, step=10, unit=UnitOfPower.WATT,
        device_class=NumberDeviceClass.POWER, icon="mdi:transmission-tower-export",
        note="Preset discharge power to the grid side. Capped by the power limit.",
    ),
    "dischargeProtection": _SystemModeNumber(
        key="dischargeProtection", label="Discharge Protection", unique_id="discharge_protection",
        minimum=0, maximum=100, step=1, unit=PERCENTAGE,
        device_class=NumberDeviceClass.BATTERY, icon="mdi:battery-alert-variant-outline",
        note=(
            "Discharging stops below the minimum SOC and only resumes once the "
            "battery is back above this threshold. Must be at least 2% above the "
            "minimum SOC; a lower value is refused rather than silently clamped."
        ),
    ),
}


class EzhiCloudSystemModeNumber(EzhiCloudEntity, NumberEntity):
    """A numeric field inside the systemMode config blob.

    They share one write path (async_set_system_mode with a single keyword),
    so they share one class -- the only differences are the range, the unit
    and the label, which live in _SystemModeNumber above.
    """

    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, device_name: str, spec: _SystemModeNumber,
                 entry_data: dict | None = None):
        super().__init__(coordinator, device_name, spec.unique_id, spec.label)
        self._spec = spec
        # Only read at write time (like PowerLimit's): Local Control is
        # optional and may come up after this entity does.
        self._entry_data = entry_data or {}
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        self._attr_icon = spec.icon

    @property
    def native_value(self) -> float | None:
        return _safe_float(
            control_config(self.coordinator.data).get(self._spec.key))

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"note": self._spec.note}

    async def async_set_native_value(self, value: float) -> None:
        # The preset output power is what Local Control replaces: the inverter
        # follows the meter, not a preset. The discharge floor is unrelated to
        # that and stays writable.
        if self._spec.key == "userSetPower" and local_control_holds_inverter(self._entry_data):
            raise HomeAssistantError(LOCAL_CONTROL_HOLDS_MESSAGE)
        try:
            async with asyncio.timeout(CLOUD_WRITE_TIMEOUT_S):
                # round(), not int(): HA validates min/max but not step, so a
                # slider landing on 20.7 would otherwise truncate to 20.
                await self.coordinator.api.async_set_system_mode(
                    **{self._spec.key: round(value)}
                )
        except TimeoutError as err:
            raise HomeAssistantError(
                f"the inverter did not answer within {CLOUD_WRITE_TIMEOUT_S} s "
                f"-- the {self._spec.label} change may or may not have been applied"
            ) from err
        except EzhiCloudError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()


class LocalControlOffsetNumber(LocalControlEntity, NumberEntity):
    """How much grid draw the inverter leaves standing (the group's offset).

    The app caps it at 10 % of the group's total power -- 120 W on this
    hardware. A higher offset is the safer side of the same error: the inverter
    covers a little less of the house load instead of risking an export.

    Changing it while the group stands re-applies the group with the new value
    under a fresh group version. When a group is first formed the inverter was
    measured to reconnect for about 11 s and to regulate after about 28 s; for
    an offset change that gap has not been timed, but expect something like it
    -- change it when it matters, not every minute.
    """

    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0
    _attr_native_max_value = max_offset()
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:arrow-collapse-vertical"

    def __init__(self, coordinator, device_name: str):
        super().__init__(coordinator, device_name, "offset", "Local Control Offset")

    @property
    def native_value(self) -> float | None:
        # What the group really runs with when there is one -- it may have
        # been changed in the vendor app -- otherwise what the next start uses.
        state = self.coordinator.data
        if state is not None and state.active and state.offset is not None:
            return state.offset
        return self.coordinator.offset

    async def async_set_native_value(self, value: float) -> None:
        try:
            offset = check_offset(value)
        except LocalControlError as err:
            raise HomeAssistantError(str(err)) from err
        coordinator = self.coordinator
        state = coordinator.data
        reapply = state is not None and state.ezhi_member
        if reapply:
            try:
                async with asyncio.timeout(LC_WRITE_TIMEOUT_S):
                    await coordinator.control.async_enable(offset, wait=False)
            except TimeoutError as err:
                raise HomeAssistantError(
                    f"the devices did not answer within {LC_WRITE_TIMEOUT_S} s "
                    "-- the new offset may or may not have been applied"
                ) from err
            except EzhiCloudError as err:
                raise HomeAssistantError(str(err)) from err
        # Stored only once the devices took it, or at once when there is no
        # group to tell: a refused write must not leave a value the next start
        # would silently use.
        coordinator.offset = offset
        entry = coordinator.config_entry
        if entry is not None:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_LOCAL_CONTROL_OFFSET: offset})
        coordinator.speed_up()
        await coordinator.async_request_refresh()

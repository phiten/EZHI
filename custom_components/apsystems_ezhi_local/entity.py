"""Shared base for the cloud-backed control entities.

Kept out of const.py: this needs Home Assistant imports (CoordinatorEntity,
DeviceInfo), and const.py stays import-light on purpose so cloud.py -- which
is deliberately free of homeassistant imports -- never has a reason to touch
this module.

Used by every entity that rides the control coordinator: switch.py, select.py,
number.py's EzhiCloudSocNumber, the sensor.py classes built on outputData and
deviceInfo, and since v0.9.0 the diagnostic binary sensors as well. The local
HTTP-API entities (the alarm sensors, the PowerLimit number, the main sensor
set) keep their own device_info variants and do not use this base.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud import control_config, wire_str
from .const import (
    CLOUD_COORDINATOR,
    DOMAIN,
    LC_COORDINATOR,
    RECONNECT_GRACE_S,
    local_setpoint_ignored_by,
)

_LOGGER = logging.getLogger(__name__)

# Deadline for a service-call write to the cloud (switch/select/number's
# async_turn_on/off, async_select_option, async_set_native_value). This is
# not cloud.py's own per-HTTP-round-trip timeout (EzhiCloudApi's self._timeout,
# 15 s default): cloud.py's ponytail: comment ties its no-wrapping-deadline
# stance to DataUpdateCoordinator being the only caller, and worst case per
# attempt is already ~4x that. select.py/number.py's writes are a GET+POST,
# so unguarded worst case is 60-120 s. Without this, a slow cloud leaves the
# service call hanging, the frontend times out, the user reads that as
# failure and presses again -- a second write to a hybrid inverter, the
# exact hazard _attr_assumed_state exists to reduce, arriving through
# another door. 30 s surfaces that risk as a clear error well before the
# true worst case.
CLOUD_WRITE_TIMEOUT_S = 30


def extend_grace(entry_data: dict, seconds: float | None = None) -> None:
    """Expect the inverter to be silent for a while: keep the last values.

    For after a command that makes it reconnect or switch its way of working --
    a system mode change above all -- when polls fail for a minute or more and
    would otherwise turn every entity unavailable (see grace.py).
    """
    for key in ("COORDINATOR", CLOUD_COORDINATOR):
        extend = getattr(entry_data.get(key), "extend_grace", None)
        if extend is not None:
            extend(RECONNECT_GRACE_S if seconds is None else seconds)


def mode_ignoring_local_writes(entry_data: dict) -> str | None:
    """Name of the system mode that will discard a local setPower, or None.

    Lives here rather than in either caller because there are two ways to write
    the setpoint -- the number entity and the set_power service -- and a rule
    that guards only one of them is worse than no rule: it teaches the log
    reader that silence means the write landed.

    None when the cloud side is not configured. Reading the mode is what makes
    the warning possible at all, and that is optional.
    """
    coordinator = entry_data.get(CLOUD_COORDINATOR)
    if coordinator is None:
        return None
    raw = control_config(coordinator.data).get("systemMode")
    return local_setpoint_ignored_by(None if raw is None else wire_str(raw))


# How long a refusal waits for the inverter to confirm a mode that is not Local.
MODE_CHECK_TIMEOUT_S = 10


async def async_require_local_mode(entry_data: dict) -> None:
    """Refuse an on-grid setpoint the inverter would ignore.

    The local `setPower` answers SUCCESS in every system mode and only acts in
    Local (see const.local_setpoint_ignored_by), so a write outside Local looks
    exactly like one that worked. This raises HomeAssistantError instead, for
    both ways of writing the setpoint -- the number entity and the set_power
    service -- because a rule that guards only one of them teaches whoever
    reads the log that silence means the write landed.

    The mode comes from the control coordinator, which can be a poll interval
    behind: someone who has just switched to Local must not be refused on the
    strength of the mode they left. So a mode that is not Local is first read
    again from the inverter, and the write is refused on that reading. Only
    when the inverter cannot be asked does the last known mode decide.

    Nothing is refused when the mode is unknown -- no control layer configured,
    or nothing read yet. Refusing on a guess would block a write that works.
    """
    mode = mode_ignoring_local_writes(entry_data)
    if mode is None:
        return
    coordinator = entry_data[CLOUD_COORDINATOR]
    fresh = False
    try:
        async with asyncio.timeout(MODE_CHECK_TIMEOUT_S):
            await coordinator.async_refresh()
    except TimeoutError:
        _LOGGER.debug("the mode check timed out; going by the last known mode")
    else:
        # A poll that failed but was covered with the last data (grace.py)
        # reads as a success to Home Assistant, and is not a fresh reading.
        fresh = (bool(getattr(coordinator, "last_update_success", True))
                 and bool(getattr(coordinator, "fresh", True)))
        mode = mode_ignoring_local_writes(entry_data)
        if mode is None:
            return
    message = (
        f"The on-grid power only takes effect in the Local system mode, and the "
        f"inverter is in {mode} mode. There it would answer SUCCESS and ignore "
        f"the value, so nothing was sent."
    )
    if not fresh:
        message += (
            " (That is the last mode read: the inverter did not answer just now. "
            "If you have only just switched to Local, try again in a moment.)"
        )
    if local_control_holds_inverter(entry_data):
        message += (
            " Local Control is active: the inverter follows the smart meter, "
            "not a setpoint. Use the Local Control Offset to change what it "
            "regulates to, or switch Local Control off and set the System Mode "
            "to Local first."
        )
    else:
        message += " Set the System Mode to Local first."
    raise HomeAssistantError(message)


def local_control_holds_inverter(entry_data: dict) -> bool:
    """Whether the inverter currently belongs to a Local Control group.

    The system mode and the preset output power are what Local Control takes
    over: the group is formed in Balcony Storage mode and the inverter follows
    the meter, not a preset. Writing either while the group stands could do
    nothing or pull the inverter out of the group -- which of the two is not
    established, so the writers refuse and name the way out.

    False when Local Control is not set up or nothing is known yet: the absence
    of information must not block a write the user could always make.
    """
    coordinator = entry_data.get(LC_COORDINATOR)
    if coordinator is None:
        return False
    # The controller knows the last command as well as the last poll, and the
    # command wins for a while: right after the switch goes on, the poll still
    # says "not in the group".
    return bool(coordinator.control.inverter_may_be_grouped(coordinator.data))


LOCAL_CONTROL_HOLDS_MESSAGE = (
    "Local Control is active: the inverter regulates to the smart meter, and "
    "this setting would conflict with that (it may also dissolve the group). "
    "Switch Local Control off first if you want to change it."
)


class EzhiCloudEntity(CoordinatorEntity):
    """Common device binding and naming for a cloud-backed control entity.

    `unique_id_suffix` and `name_suffix` are the only things that differ
    between the on/off switch, the system-mode select and the two SOC
    numbers -- everything else (device identity, manufacturer/model, the
    "apsystems_<device>_cloud_<suffix>" unique-id convention) was previously
    copied verbatim into all three.
    """

    # The name is the entity's alone ("Backup Power"); Home Assistant puts the
    # device name in front of it. Baking it in here as well produced "EZHI
    # APsystems EZHI Backup Power" wherever the frontend shows both.
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        device_name: str,
        unique_id_suffix: str,
        name_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._attr_name = name_suffix
        self._attr_unique_id = f"apsystems_{device_name}_cloud_{unique_id_suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_name)},
            name=self._device_name,
            manufacturer="APsystems",
            model="EZHI",
        )


class LocalControlEntity(CoordinatorEntity):
    """An entity on the Local Control coordinator, shown on the inverter's device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, device_name: str, suffix: str, name: str) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._attr_name = name
        self._attr_unique_id = f"apsystems_{device_name}_local_control_{suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_name)},
            name=self._device_name,
            manufacturer="APsystems",
            model="EZHI",
        )


def async_register_sem_device(hass, entry, device_name: str, sem_id: str) -> None:
    """Create the smart meter's device and attach it to the inverter's.

    Done in the device registry, with the inverter's registry id, instead of
    through `DeviceInfo(via_device=(DOMAIN, name))`: Home Assistant is retiring
    that identifier pair (identifiers are only unique per config entry) in favour
    of `via_device_id` and warns about the old form. The entities then name the
    device by its identifiers alone and find it already linked.
    """
    registry = dr.async_get(hass)
    inverter = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device_name)},
        name=device_name,
        manufacturer="APsystems",
        model="EZHI",
    )
    meter = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"sem_{sem_id}")},
        name=f"{device_name} Smart Meter",
        manufacturer="APsystems",
        model="SEM",
        serial_number=sem_id,
    )
    if meter.via_device_id != inverter.id:
        registry.async_update_device(meter.id, via_device_id=inverter.id)


class SemEntity(CoordinatorEntity):
    """An entity fed by the smart meter's pushed readings.

    The meter is its own device in Home Assistant, attached to the inverter
    because it is the inverter's group partner and cannot be used without it
    here. The attachment is made by async_register_sem_device.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator, device_name: str, sem_id: str, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._sem_id = sem_id
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"apsystems_sem_{sem_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"sem_{self._sem_id}")},
            name=f"{self._device_name} Smart Meter",
            manufacturer="APsystems",
            model="SEM",
            serial_number=self._sem_id,
        )

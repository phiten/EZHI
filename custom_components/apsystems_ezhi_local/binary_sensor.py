"""Binary sensor platform for APsystems EZHI local API integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant import config_entries
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_NAME,
    CONF_NAME,
    EVENT_LOGBOOK_ENTRY,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ApSystemsDataCoordinator
from .alarm_texts import alarm_text
from .api import ReturnAlarmData
from .ble_api import ble_device_available
from .cloud import control_device, control_extras
from .const import (
    CLOUD_COORDINATOR,
    DOMAIN,
    LC_COORDINATOR,
    TRANSPORT_BLUETOOTH,
    TRANSPORT_LOCAL_MQTT,
    resolve_transport,
)
from .device_fields import (
    EXTRA_BINARY_FIELDS,
    INFO_BINARY_FIELDS,
    ExtraField,
    InfoField,
    extra_value,
    info_value,
)
from .entity import EzhiCloudEntity, LocalControlEntity
from .local_control import PROBLEM_UNREADABLE


@dataclass(frozen=True, kw_only=True)
class EZHIBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes EZHI binary sensor entity."""
    
    # The getAlarm field this sensor reads, which is also the key into
    # ALARM_TEXTS. Carried explicitly rather than parsed back out of value_fn.
    alarm_code: str

    # bool | None, not bool: the three newest alarm fields are absent on
    # older firmware, and "we don't know" is not the same answer as "no".
    value_fn: Callable[[ReturnAlarmData], bool | None]


def _alarm_flag(raw) -> bool | None:
    """One alarm field to on/off, or None when the firmware did not report it.

    The older sensors in this file compare to "1" directly and so read a
    missing field as "off". For a field that may genuinely be absent, that
    would assert the absence of a fault the device never denied.
    """
    if raw is None or str(raw) == "":
        return None
    return str(raw) == "1"


ALARM_SENSORS: tuple[EZHIBinarySensorEntityDescription, ...] = (
    EZHIBinarySensorEntityDescription(
        key="battery_overtemp",
        name="Battery Overtemperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="BatHTP",
        value_fn=lambda data: str(data.BatHTP) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="battery_undertemp",
        name="Battery Undertemperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="BatLTP",
        value_fn=lambda data: str(data.BatLTP) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="battery_comm_error",
        name="Battery Communication Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="BatCE",
        value_fn=lambda data: str(data.BatCE) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="battery_overvoltage",
        name="Battery Overvoltage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="BatHV",
        value_fn=lambda data: str(data.BatHV) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="battery_undervoltage",
        name="Battery Undervoltage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="BatLV",
        value_fn=lambda data: str(data.BatLV) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="battery_overcurrent",
        name="Battery Overcurrent",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="BatHI",
        value_fn=lambda data: str(data.BatHI) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="battery_error",
        name="Battery Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="BatE",
        value_fn=lambda data: str(data.BatE) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="battery_shutdown",
        name="Battery Shutdown",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="SBS",
        value_fn=lambda data: str(data.SBS) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="device_overtemp",
        name="Device Overtemperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="DTP",
        value_fn=lambda data: str(data.DTP) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="device_error",
        name="Device Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="EE",
        value_fn=lambda data: str(data.EE) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="ac_abnormal",
        name="AC Abnormal",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="ACA",
        value_fn=lambda data: str(data.ACA) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="offgrid_overcurrent",
        name="Off-Grid Overcurrent",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="OfOI",
        value_fn=lambda data: str(data.OfOI) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="offgrid_short",
        name="Off-Grid Short Circuit",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="OfGS",
        value_fn=lambda data: str(data.OfGS) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="pv_overvoltage",
        name="PV Overvoltage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="PvHV",
        value_fn=lambda data: str(data.PvHV) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="pv_overcurrent",
        name="PV Overcurrent",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="PvOC",
        value_fn=lambda data: str(data.PvOC) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="pv_wiring_error",
        name="PV Wiring Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="PVWE",
        value_fn=lambda data: str(data.PVWE) == "1",
    ),
    EZHIBinarySensorEntityDescription(
        key="ird_error",
        name="IRD Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="IRDE",
        value_fn=lambda data: str(data.IRDE) == "1",
    ),
    # The three fields getAlarm reports but the integration never mapped.
    # Names and meanings are the vendor app's own, not invented here.
    EZHIBinarySensorEntityDescription(
        key="soc_calibration",
        name="SOC Calibration Needed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="BCC",
        value_fn=lambda data: _alarm_flag(data.BCC),
    ),
    EZHIBinarySensorEntityDescription(
        key="battery_access_conflict",
        name="Battery Access Conflict",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="BCI",
        value_fn=lambda data: _alarm_flag(data.BCI),
    ),
    EZHIBinarySensorEntityDescription(
        key="voltage_reset_protection",
        name="Voltage Reset Protection",
        device_class=BinarySensorDeviceClass.PROBLEM,
        alarm_code="VRP",
        value_fn=lambda data: _alarm_flag(data.VRP),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""
    config = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = config["COORDINATOR"]

    lc_coordinator = config.get(LC_COORDINATOR)
    if lc_coordinator is not None:
        add_entities([LocalControlProblemSensor(lc_coordinator, config[CONF_NAME])])

    add_entities(
        EZHIAlarmBinarySensor(
            coordinator=coordinator,
            description=description,
            device_name=config[CONF_NAME],
        )
        for description in ALARM_SENSORS
    )

    # The link flags ride the control coordinator, not the local one: they come
    # out of deviceInfo, which only the local transports read. Same gating as
    # the sensor platform -- on the cloud transport `device` is always {}, so
    # these would be permanently unavailable rather than merely empty.
    cloud_coordinator = config.get(CLOUD_COORDINATOR)
    if cloud_coordinator is None:
        return
    transport = resolve_transport(config)
    cloud_entities: list[BinarySensorEntity] = []
    if transport in (TRANSPORT_BLUETOOTH, TRANSPORT_LOCAL_MQTT):
        cloud_entities.extend(
            EzhiInfoBinarySensor(cloud_coordinator, config[CONF_NAME], field)
            for field in INFO_BINARY_FIELDS
        )
    # supportFunction, meterStatus and btLock are MQTT-only -- see
    # mqtt_api.async_poll_all.
    if transport == TRANSPORT_LOCAL_MQTT:
        cloud_entities.extend(
            EzhiExtraBinarySensor(cloud_coordinator, config[CONF_NAME], field)
            for field in EXTRA_BINARY_FIELDS
        )
    add_entities(cloud_entities)


class EzhiInfoBinarySensor(EzhiCloudEntity, BinarySensorEntity):
    """One "1"/"0" deviceInfo field: the cloud link, the WiFi link, and the two
    Bluetooth flags.

    isOnline is the device's own view of its vendor-cloud connection, which is
    the only honest source for it -- an install that reroutes the vendor broker
    to a local one reads 0 here, and that is a fact worth seeing rather than a
    fault. btEnable answers "why does the Bluetooth transport not work?" in one
    look, which is why it is on by default despite being static.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device_name: str, field: InfoField) -> None:
        super().__init__(coordinator, device_name, field.uid, field.name)
        self._field = field
        if field.device_class is not None:
            self._attr_device_class = BinarySensorDeviceClass(field.device_class)
        if not field.enabled:
            self._attr_entity_registry_enabled_default = False

    @property
    def available(self) -> bool:
        return super().available and ble_device_available(self.coordinator.data)

    @property
    def is_on(self) -> bool | None:
        value = info_value(control_device(self.coordinator.data), self._field.key)
        # None, not False, when the field is absent: "the device did not say"
        # is not the same answer as "no", and on a connectivity sensor the
        # difference is an alarm nobody asked for.
        return None if value is None else str(value) == "1"


class EzhiExtraBinarySensor(EzhiCloudEntity, BinarySensorEntity):
    """One "1"/"0" field out of supportFunction, meterStatus or btLock.

    The supportFunction flags are the firmware's own answer to "which features
    does this unit admit to" -- static, which is why they are off by default,
    but the first thing worth checking when a service call is refused.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device_name: str, field: ExtraField) -> None:
        super().__init__(coordinator, device_name, field.uid, field.name)
        self._field = field
        if field.device_class is not None:
            self._attr_device_class = BinarySensorDeviceClass(field.device_class)
        if not field.enabled:
            self._attr_entity_registry_enabled_default = False

    @property
    def available(self) -> bool:
        return super().available and self._field.identifier in control_extras(
            self.coordinator.data)

    @property
    def is_on(self) -> bool | None:
        value = extra_value(control_extras(self.coordinator.data), self._field)
        return None if value is None else str(value) == "1"


class EZHIAlarmBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of an EZHI alarm binary sensor."""

    entity_description: EZHIBinarySensorEntityDescription

    # See EzhiCloudEntity: with this set the name is entity_description.name
    # alone, and Home Assistant prefixes the device name itself.
    _attr_has_entity_name = True

    # These never change, and there are twenty of these entities: recording a
    # paragraph of static text on every alarm poll would grow the database for
    # nothing.
    _unrecorded_attributes = frozenset(
        {"alarm_code", "vendor_name", "cause", "suggested_action"}
    )

    def __init__(
        self,
        coordinator: ApSystemsDataCoordinator,
        description: EZHIBinarySensorEntityDescription,
        device_name: str,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._device_name = device_name
        self._attr_unique_id = f"apsystems_{device_name}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self.coordinator.alarm_data is None:
            return None
        try:
            return self.entity_description.value_fn(self.coordinator.alarm_data)
        except (AttributeError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """What the vendor app would tell you about this alarm.

        Carried whether or not the alarm is active: someone looking at a
        problem sensor wants to know what it would mean before it fires, and
        an attribute that only appears during a fault is one nobody finds when
        they need it.
        """
        code = self.entity_description.alarm_code
        text = alarm_text(code, self.hass.config.language if self.hass else None)
        attrs = {"alarm_code": code}
        if text.get("name"):
            attrs["vendor_name"] = text["name"]
        if text.get("reason"):
            attrs["cause"] = text["reason"]
        if text.get("suggest"):
            attrs["suggested_action"] = text["suggest"]
        return attrs

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        info = DeviceInfo(
            identifiers={(DOMAIN, self._device_name)},
            name=self._device_name,
            manufacturer="APsystems",
            model="EZHI",
        )
        
        # Add dynamic info from coordinator if available
        if self.coordinator.device_info is not None:
            dev = self.coordinator.device_info
            if dev.devVer:
                info["sw_version"] = dev.devVer
            if dev.deviceId:
                info["serial_number"] = dev.deviceId
            if dev.ip:
                info["configuration_url"] = f"http://{dev.ip}/getDeviceInfo"
        
        return info


class LocalControlProblemSensor(LocalControlEntity, BinarySensorEntity):
    """On when a Local Control group was asked for but is not working.

    Off while no group exists: switched off is not a fault. On, too, when the
    devices cannot be read at all -- a meter that lost power is exactly the case
    this sensor is for, and it must not go "unavailable" then, which an alert on
    `state == on` would never see.

    The attributes say why: `reason` is a sentence, `cause` a fixed word for
    automations (inverter_only, meter_only, mismatch, no_data, unreadable), and
    while the sensor is on the values the verdict was reached from are listed
    next to them. `last_problem` and `last_problem_at` stay after the problem has
    gone, so a fault that cleared by itself can still be told apart from none.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    # Long, and they change only when the problem does.
    _unrecorded_attributes = frozenset({"reason", "last_problem", "differences"})

    def __init__(self, coordinator, device_name: str):
        super().__init__(coordinator, device_name, "problem", "Local Control Problem")
        # (cause, silent devices) of the problem last written to the logbook.
        self._logged: tuple | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._log_to_logbook()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._log_to_logbook()
        super()._handle_coordinator_update()

    def _log_to_logbook(self) -> None:
        """Write the reason into the logbook, next to the state change.

        The logbook shows a binary sensor's change as "Problem" and has no place
        for attributes, so "why" would stay out of sight there. One entry per
        problem, and another when its cause changes (the inverter was silent,
        now both are); not again while the same one goes on, and nothing when it
        goes away -- the sensor's own change to "OK" says that.
        """
        if self.hass is None or not self.entity_id:
            return
        problem = self.coordinator.problem
        key = None if problem is None else (problem.code, problem.devices)
        if key == self._logged:
            return
        self._logged = key
        if problem is None:
            return
        self.hass.bus.async_fire(
            EVENT_LOGBOOK_ENTRY,
            {
                ATTR_NAME: f"{self._device_name} {self._attr_name}",
                # The logbook's own field name; its constants live in a
                # component that need not be loaded. The domain, and with it
                # the icon, it takes from the entity id.
                "message": problem.summary,
                ATTR_ENTITY_ID: self.entity_id,
            },
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool | None:
        coordinator = self.coordinator
        if coordinator.last_update_success and coordinator.data is None:
            return None
        if not coordinator.last_update_success and coordinator.last_exception is None:
            return None
        return coordinator.problem is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator = self.coordinator
        attrs: dict[str, Any] = {}
        problem = coordinator.problem
        if problem is not None:
            attrs["reason"] = problem.text
            attrs["cause"] = problem.code
            if problem.devices:
                attrs["silent_devices"] = list(problem.devices)
            state = coordinator.data
            if state is not None and problem.code != PROBLEM_UNREADABLE:
                attrs.update({
                    "inverter_in_group": state.ezhi_member,
                    "meter_in_group": state.sem_member,
                    "inverter_third_link": state.third_link,
                    "meter_link_status": state.sem_status,
                    "configs_match": state.consistent,
                    "seconds_without_meter_data": state.no_data_count,
                })
                if state.mismatch:
                    attrs["differences"] = list(state.mismatch)
        if coordinator.last_problem is not None:
            attrs["last_problem"] = coordinator.last_problem.text
            attrs["last_problem_at"] = coordinator.last_problem_at.isoformat()
        return attrs

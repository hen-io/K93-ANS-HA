from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHANNELS,
    CONF_CHATROOMS,
    CONF_RECIPIENTS,
    DOMAIN,
    SIGNAL_DELETED,
    SIGNAL_UPDATED,
)
from .models import NotificationRecord
from .store import NotificationStore


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    store: NotificationStore = hass.data[DOMAIN][entry.entry_id]["store"]
    async_add_entities(
        [
            K93AnsChannelsSensor(entry, store),
            K93AnsRecipientsSensor(entry, store),
            K93AnsChatroomsSensor(entry, store),
            K93AnsStoredSensor(entry, store),
            K93AnsSentTodaySensor(entry, store),
            K93AnsSentThisWeekSensor(entry, store),
            K93AnsSentThisMonthSensor(entry, store),
            K93AnsUnacknowledgedSensor(entry, store),
            K93AnsDatabaseSizeSensor(entry, store),
        ]
    )


def _local_date(record: NotificationRecord):
    return dt_util.as_local(dt_util.parse_datetime(record["created"])).date()


def _history_records(store: NotificationStore) -> list[NotificationRecord]:
    return [r for r in store.async_list() if r.get("show_in_history", True)]


class K93AnsSensorBase(SensorEntity):

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, store: NotificationStore, key: str, name: str) -> None:
        self._entry = entry
        self._store = store
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="K93 ANS",
            manufacturer="@hen-io | Henrik Isefjær Olsen",
            model="Advanced Notification System",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await self._async_refresh()

        async def _on_signal(*_args: Any) -> None:
            await self._async_refresh()

        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, _on_signal))
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_DELETED, _on_signal))

    async def _async_refresh(self) -> None:
        self.async_write_ha_state()


class K93AnsChannelsSensor(K93AnsSensorBase):

    _attr_icon = "mdi:tune-vertical"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, store: NotificationStore) -> None:
        super().__init__(entry, store, "channels", "Configured channels")

    @property
    def native_value(self) -> int:
        return len(self._entry.options.get(CONF_CHANNELS, []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "channels": [
                {
                    "key": channel["key"],
                    "name": channel["name"],
                    "min_importance": channel["min_importance"],
                    "enabled": channel.get("enabled", True),
                    "color": channel.get("color"),
                }
                for channel in self._entry.options.get(CONF_CHANNELS, [])
            ]
        }


class K93AnsRecipientsSensor(K93AnsSensorBase):

    _attr_icon = "mdi:account-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, store: NotificationStore) -> None:
        super().__init__(entry, store, "recipients", "Configured recipients")

    @property
    def native_value(self) -> int:
        return len(self._entry.options.get(CONF_RECIPIENTS, []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "recipients": [
                {
                    "name": recipient["name"],
                    "notify_service": recipient["notify_service"],
                    "min_importance": recipient["min_importance"],
                    "allowed_channels": recipient.get("allowed_channels") or [],
                    "person_entity_id": recipient.get("person_entity_id"),
                    "enabled": recipient.get("enabled", True),
                }
                for recipient in self._entry.options.get(CONF_RECIPIENTS, [])
            ]
        }


class K93AnsChatroomsSensor(K93AnsSensorBase):

    _attr_icon = "mdi:forum"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, store: NotificationStore) -> None:
        super().__init__(entry, store, "chatrooms", "Configured chatrooms")

    @property
    def native_value(self) -> int:
        return len(self._entry.options.get(CONF_CHATROOMS, []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "chatrooms": [
                {
                    "id": room["id"],
                    "name": room["name"],
                    "icon": room.get("icon"),
                    "enabled": room.get("enabled", True),
                }
                for room in self._entry.options.get(CONF_CHATROOMS, [])
            ]
        }


class K93AnsStoredSensor(K93AnsSensorBase):

    _attr_icon = "mdi:database"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, store: NotificationStore) -> None:
        super().__init__(entry, store, "stored", "Notifications stored")

    @property
    def native_value(self) -> int:
        return len(_history_records(self._store))


class K93AnsSentTodaySensor(K93AnsSensorBase):

    _attr_icon = "mdi:calendar-today"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, store: NotificationStore) -> None:
        super().__init__(entry, store, "sent_today", "Sent today")

    @property
    def native_value(self) -> int:
        today = dt_util.now().date()
        return len([r for r in _history_records(self._store) if _local_date(r) == today])


class K93AnsSentThisWeekSensor(K93AnsSensorBase):

    _attr_icon = "mdi:calendar-week"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, store: NotificationStore) -> None:
        super().__init__(entry, store, "sent_this_week", "Sent this week")

    @property
    def native_value(self) -> int:
        today = dt_util.now().date()
        monday = today - timedelta(days=today.weekday())
        return len([r for r in _history_records(self._store) if _local_date(r) >= monday])


class K93AnsSentThisMonthSensor(K93AnsSensorBase):

    _attr_icon = "mdi:calendar-month"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, store: NotificationStore) -> None:
        super().__init__(entry, store, "sent_this_month", "Sent this month")

    @property
    def native_value(self) -> int:
        today = dt_util.now().date()
        count = 0
        for record in _history_records(self._store):
            local_date = _local_date(record)
            if local_date.year == today.year and local_date.month == today.month:
                count += 1
        return count


class K93AnsUnacknowledgedSensor(K93AnsSensorBase):

    _attr_icon = "mdi:bell-alert"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, store: NotificationStore) -> None:
        super().__init__(entry, store, "unacknowledged", "Unacknowledged notifications")

    @property
    def native_value(self) -> int:
        return len(
            [r for r in self._store.async_list() if r["requires_ack"] and not r["acknowledged"]]
        )


class K93AnsDatabaseSizeSensor(K93AnsSensorBase):

    _attr_icon = "mdi:database-outline"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.KILOBYTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, store: NotificationStore) -> None:
        super().__init__(entry, store, "database_size", "Database size")
        self._size_bytes = 0

    async def _async_refresh(self) -> None:
        self._size_bytes = await self.hass.async_add_executor_job(self._store.file_size_bytes)
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return self._size_bytes

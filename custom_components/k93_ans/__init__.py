from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    ACK_ACTION_PREFIX,
    CONF_CHANNELS,
    CONF_HISTORY_MAX_RECORDS,
    CONF_HISTORY_RETENTION_DAYS,
    CONF_STORAGE_PATH,
    DEFAULT_HISTORY_MAX_RECORDS,
    DEFAULT_HISTORY_RETENTION_DAYS,
    DOMAIN,
    EVENT_NOTIFICATION,
)
from .calendar_scheduler import async_setup_calendar_notifications
from .config_snapshot import async_write_config_snapshot
from .dispatch import (
    async_acknowledge,
    async_clear_inactive_live_recipients,
    async_clear_live_notifications_on_startup,
    async_handle_notification_event,
    async_register_persistent_notification_listener,
    async_restore_persistent_notifications,
)
from .image_capture import async_prune_orphaned_images
from .scheduler import async_setup_scheduled_notifications
from .services import async_register_services, async_unregister_services
from .store import NotificationStore
from .websocket_api import async_register_websocket_api

PLATFORMS: list[str] = ["sensor"]

MOBILE_APP_ACTION_EVENT = "mobile_app_notification_action"
PRUNE_INTERVAL = timedelta(hours=1)
INACTIVITY_CHECK_INTERVAL = timedelta(minutes=1)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    store = NotificationStore(hass, entry.options.get(CONF_STORAGE_PATH))
    await store.async_load()
    await async_write_config_snapshot(hass, store, entry.options)
    await async_clear_live_notifications_on_startup(hass, store)
    async_restore_persistent_notifications(hass, store)

    async def _on_notification_event(event: Event) -> None:
        await async_handle_notification_event(hass, entry, store, event)

    async def _on_mobile_action(event: Event) -> None:
        action = event.data.get("action")
        if isinstance(action, str) and action.startswith(ACK_ACTION_PREFIX):
            notification_id = action[len(ACK_ACTION_PREFIX) :]
            await async_acknowledge(hass, store, notification_id, "mobile_action")

    async def _on_prune(_now) -> None:
        await store.async_prune(
            entry.options.get(CONF_CHANNELS, []),
            entry.options.get(CONF_HISTORY_MAX_RECORDS, DEFAULT_HISTORY_MAX_RECORDS),
            entry.options.get(CONF_HISTORY_RETENTION_DAYS, DEFAULT_HISTORY_RETENTION_DAYS),
        )
        await async_prune_orphaned_images(hass, store)

    async def _on_check_inactive(_now) -> None:
        await async_clear_inactive_live_recipients(hass, entry, store)

    unsub_event = hass.bus.async_listen(EVENT_NOTIFICATION, _on_notification_event)
    unsub_action = hass.bus.async_listen(MOBILE_APP_ACTION_EVENT, _on_mobile_action)
    unsub_prune = async_track_time_interval(hass, _on_prune, PRUNE_INTERVAL)
    unsub_inactive = async_track_time_interval(hass, _on_check_inactive, INACTIVITY_CHECK_INTERVAL)
    unsub_persistent = async_register_persistent_notification_listener(hass, store)
    unsub_scheduled = async_setup_scheduled_notifications(hass, entry, store)
    unsub_calendar = async_setup_calendar_notifications(hass, entry, store)
    unsub_options_update = entry.add_update_listener(_async_reload_entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "store": store,
        "unsub_event": unsub_event,
        "unsub_action": unsub_action,
        "unsub_prune": unsub_prune,
        "unsub_inactive": unsub_inactive,
        "unsub_persistent": unsub_persistent,
        "unsub_scheduled": unsub_scheduled,
        "unsub_calendar": unsub_calendar,
        "unsub_options_update": unsub_options_update,
    }

    async_register_services(hass, entry, store)
    async_register_websocket_api(hass, store)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    entry_data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if entry_data is not None:
        entry_data["unsub_event"]()
        entry_data["unsub_action"]()
        entry_data["unsub_prune"]()
        entry_data["unsub_inactive"]()
        entry_data["unsub_persistent"]()
        entry_data["unsub_scheduled"]()
        entry_data["unsub_calendar"]()
        entry_data["unsub_options_update"]()
    async_unregister_services(hass)
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

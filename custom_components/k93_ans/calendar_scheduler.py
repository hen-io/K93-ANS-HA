from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.helpers.event import async_track_point_in_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import CONF_CALENDAR_NOTIFICATIONS, DEFAULT_ALL_DAY_TIME, DEFAULT_CHANNEL
from .services import async_send_notification
from .store import NotificationStore

_LOGGER = logging.getLogger(__name__)


def _notification_data(calendar_notification: dict[str, Any], state: State) -> dict[str, Any]:
    attrs = state.attributes
    title = calendar_notification.get("title") or attrs.get("message") or calendar_notification["name"]
    message = calendar_notification.get("message") or attrs.get("description") or attrs.get("message") or ""
    return {
        "title": title,
        "message": message,
        "icon": calendar_notification.get("icon"),
        "image": None,
        "channel": calendar_notification.get("channel") or DEFAULT_CHANNEL,
        "importance": calendar_notification.get("importance", "normal"),
        "actions": [],
        "persistent": calendar_notification.get("persistent", False),
        "data": {},
        "target_recipients": calendar_notification.get("target_recipients") or None,
        "home_only": calendar_notification.get("home_only", False),
        "live_id": None,
        "interactive_only": False,
        "source": f"calendar:{calendar_notification.get('name', calendar_notification.get('id'))}",
        "show_in_history": True,
        "dismiss_on_action": False,
        "clear_on_acknowledge": True,
    }


def _all_day_target(all_day_time: str, now: datetime) -> datetime:
    hour, minute, second = (int(part) for part in all_day_time.split(":"))
    return now.replace(hour=hour, minute=minute, second=second, microsecond=0)


def async_setup_calendar_notifications(
    hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore
) -> Callable[[], None]:
    unsubs: list[Callable[[], None]] = []
    pending_all_day: dict[str, Callable[[], None]] = {}

    def _cancel_pending(config_id: str) -> None:
        unsub = pending_all_day.pop(config_id, None)
        if unsub is not None:
            unsub()

    async def _fire(calendar_notification: dict[str, Any], state: State) -> None:
        await async_send_notification(
            hass, entry, store, _notification_data(calendar_notification, state)
        )

    async def _handle_active(calendar_notification: dict[str, Any], state: State) -> None:
        config_id = calendar_notification["id"]
        if not state.attributes.get("all_day"):
            await _fire(calendar_notification, state)
            return

        all_day_time = calendar_notification.get("all_day_time") or DEFAULT_ALL_DAY_TIME
        now = dt_util.now()
        target = _all_day_target(all_day_time, now)
        if target <= now:
            await _fire(calendar_notification, state)
            return

        _cancel_pending(config_id)

        async def _fire_if_still_active(_now: datetime) -> None:
            pending_all_day.pop(config_id, None)
            current = hass.states.get(calendar_notification["calendar_entity"])
            if current is not None and current.state == "on":
                await _fire(calendar_notification, current)

        pending_all_day[config_id] = async_track_point_in_time(
            hass, _fire_if_still_active, dt_util.as_utc(target)
        )

    def _watch(calendar_notification: dict[str, Any]) -> None:
        entity_id = calendar_notification.get("calendar_entity")
        if not entity_id:
            return

        async def _on_state_change(event: Event) -> None:
            new_state = event.data["new_state"]
            old_state = event.data["old_state"]
            if new_state is None or new_state.state != "on":
                return
            if old_state is not None and old_state.state == "on":
                return
            await _handle_active(calendar_notification, new_state)

        unsubs.append(async_track_state_change_event(hass, [entity_id], _on_state_change))

        current = hass.states.get(entity_id)
        if current is not None and current.state == "on" and current.attributes.get("all_day"):
            hass.async_create_task(_handle_active(calendar_notification, current))

    for calendar_notification in entry.options.get(CONF_CALENDAR_NOTIFICATIONS, []):
        if calendar_notification.get("enabled", True):
            _watch(calendar_notification)

    def _unsub_all() -> None:
        for unsub in unsubs:
            unsub()
        unsubs.clear()
        for unsub in list(pending_all_day.values()):
            unsub()
        pending_all_day.clear()

    return _unsub_all

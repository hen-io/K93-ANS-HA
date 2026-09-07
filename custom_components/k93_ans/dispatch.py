from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.components.persistent_notification import (
    SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED,
    UpdateType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_HOME, STATE_OFF
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    ANDROID_IMPORTANCE_MAP,
    CONF_CHANNELS,
    CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES,
    CONF_RECIPIENTS,
    DEFAULT_CHANNEL,
    IMPORTANCE_LEVELS,
    IOS_INTERRUPTION_MAP,
    SIGNAL_DELETED,
    SIGNAL_UPDATED,
)
from .models import NotificationRecord
from .store import NotificationStore

_LOGGER = logging.getLogger(__name__)

_FALLBACK_CHANNEL = {
    "key": DEFAULT_CHANNEL,
    "name": DEFAULT_CHANNEL,
    "min_importance": "low",
    "enabled": True,
}


def _importance_rank(level: str) -> int:
    try:
        return IMPORTANCE_LEVELS.index(level)
    except ValueError:
        return IMPORTANCE_LEVELS.index("normal")


def _is_mobile_app_target(notify_service: str) -> bool:
    return notify_service.startswith("mobile_app_")


def _recipient_targeted(recipient: dict[str, Any], target_recipients: list[str]) -> bool:
    targets = {t.strip().lower() for t in target_recipients if t and t.strip()}
    return (
        recipient["id"].lower() in targets
        or recipient.get("name", "").strip().lower() in targets
    )


def _is_home(hass: HomeAssistant, recipient: dict[str, Any]) -> bool:
    person_entity_id = recipient.get("person_entity_id")
    if not person_entity_id:
        return True
    state = hass.states.get(person_entity_id)
    return state is not None and state.state == STATE_HOME


def _resolve_image(record: NotificationRecord) -> str | None:
    image = record.get("image")
    if image:
        return image
    icon = record.get("icon")
    if icon and not icon.startswith("mdi:"):
        return icon
    return None


def _create_persistent_notification(hass: HomeAssistant, record: NotificationRecord) -> None:
    message = record["message"]
    image = _resolve_image(record)
    if image:
        message = f"{message}\n\n![]({image})"
    persistent_notification.async_create(
        hass, message, title=record["title"], notification_id=record["id"]
    )


def _build_notify_payload(record: NotificationRecord) -> dict[str, Any]:
    return {
        "title": record["title"],
        "message": record["message"],
        "data": dict(record.get("data") or {}),
    }


def _build_mobile_app_payload(record: NotificationRecord, channel_name: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tag": record["id"],
        "channel": channel_name,
        "importance": ANDROID_IMPORTANCE_MAP.get(record["importance"], "default"),
        "push": {
            "interruption-level": IOS_INTERRUPTION_MAP.get(record["importance"], "active")
        },
    }
    if record.get("actions"):
        data["actions"] = record["actions"]
        if not record.get("dismiss_on_action"):
            data["sticky"] = True
    if record.get("persistent"):
        data["sticky"] = True
        data["persistent"] = True

    icon = record.get("icon")
    if icon and icon.startswith("mdi:"):
        data["notification_icon"] = icon

    image = _resolve_image(record)
    if image:
        data["image"] = image

    data.update(record.get("data") or {})

    return {"title": record["title"], "message": record["message"], "data": data}


async def async_handle_notification_event(
    hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore, event: Event
) -> None:
    record: NotificationRecord = dict(event.data)

    previous = store.async_get(record["id"])
    previous_deliveries: dict[str, Any] = (previous or {}).get("recipients") or {}

    channel_defs = {c["key"]: c for c in entry.options.get(CONF_CHANNELS, [])}
    recipients = entry.options.get(CONF_RECIPIENTS, [])

    record_channel_keys = record.get("channels") or [record["channel"]]
    resolved_channels = []
    for key in record_channel_keys:
        channel = channel_defs.get(key)
        if channel is None:
            if key != DEFAULT_CHANNEL:
                _LOGGER.warning("K93 ANS: unknown channel '%s', falling back to default", key)
            channel = _FALLBACK_CHANNEL
        resolved_channels.append(channel)
    primary_channel = resolved_channels[0]

    record_rank = _importance_rank(record["importance"])
    target_recipients = record.get("target_recipients")

    deliveries: dict[str, Any] = {}
    for recipient in recipients:
        if not recipient.get("enabled", True):
            continue
        if target_recipients and not _recipient_targeted(recipient, target_recipients):
            continue

        recipient_channel_importance = recipient.get("channel_importance") or {}
        allowed_channels = recipient.get("allowed_channels") or []
        present_ok = not record.get("home_only") or _is_home(hass, recipient)
        matched = present_ok and any(
            channel.get("enabled", True)
            and (not allowed_channels or channel["key"] in allowed_channels)
            and record_rank >= max(
                _importance_rank(
                    recipient_channel_importance.get(channel["key"]) or recipient["min_importance"]
                ),
                _importance_rank(channel["min_importance"]),
            )
            for channel in resolved_channels
        )

        prior = previous_deliveries.get(recipient["id"])
        delivery: dict[str, Any] = {
            "notify_service": recipient["notify_service"],
            "matched": matched,
            "dispatched": bool(prior and prior.get("dispatched")),
            "dispatch_error": None,
            "dispatched_at": (prior or {}).get("dispatched_at"),
        }

        if matched:
            if _is_mobile_app_target(recipient["notify_service"]):
                payload = _build_mobile_app_payload(record, primary_channel["name"])
            else:
                payload = _build_notify_payload(record)
            try:
                await hass.services.async_call(
                    "notify", recipient["notify_service"], payload, blocking=False
                )
                delivery["dispatched"] = True
                delivery["dispatched_at"] = dt_util.utcnow().isoformat()
            except ServiceNotFound as err:
                delivery["dispatch_error"] = str(err)
                _LOGGER.warning(
                    "K93 ANS could not deliver to notify.%s: %s",
                    recipient["notify_service"],
                    err,
                )
            except Exception as err:
                delivery["dispatch_error"] = str(err)
                _LOGGER.exception(
                    "K93 ANS failed delivering to notify.%s", recipient["notify_service"]
                )

        deliveries[recipient["id"]] = delivery

    for recipient_id, prior_delivery in previous_deliveries.items():
        deliveries.setdefault(recipient_id, prior_delivery)

    record["recipients"] = deliveries

    if record.get("persistent"):
        _create_persistent_notification(hass, record)

    await store.async_add(record)
    async_dispatcher_send(hass, SIGNAL_UPDATED, record)


def async_restore_persistent_notifications(hass: HomeAssistant, store: NotificationStore) -> None:
    for record in store.async_list():
        if record.get("persistent") and not record.get("acknowledged"):
            _create_persistent_notification(hass, record)


async def async_clear_live_notifications_on_startup(
    hass: HomeAssistant, store: NotificationStore
) -> None:
    for record in store.async_list():
        if record.get("live_id") and not record.get("acknowledged"):
            await async_acknowledge(hass, store, record["id"], "restart")


async def _send_clear_notification(hass: HomeAssistant, notify_service: str, record_id: str) -> None:
    for attempt in (1, 2):
        try:
            _LOGGER.debug(
                "K93 ANS sending clear_notification to notify.%s for %s (attempt %d)",
                notify_service,
                record_id,
                attempt,
            )
            await hass.services.async_call(
                "notify",
                notify_service,
                {"message": "clear_notification", "data": {"tag": record_id}},
                blocking=True,
            )
            return
        except Exception:
            if attempt == 1:
                _LOGGER.warning(
                    "K93 ANS clear_notification to notify.%s failed, retrying once",
                    notify_service,
                )
                await asyncio.sleep(2)
            else:
                _LOGGER.exception(
                    "K93 ANS failed clearing pushed notification on notify.%s", notify_service
                )


async def _clear_mobile_notifications(hass: HomeAssistant, record: NotificationRecord) -> None:
    recipients = record.get("recipients") or {}
    _LOGGER.debug(
        "K93 ANS clearing pushed notifications for %s: recipients=%s",
        record["id"],
        recipients,
    )
    for delivery in recipients.values():
        notify_service = delivery.get("notify_service")
        if not delivery.get("dispatched") or not notify_service:
            _LOGGER.warning(
                "K93 ANS: not clearing %s on notify.%s - it was never dispatched there "
                "(dispatched=%s)",
                record["id"],
                notify_service,
                delivery.get("dispatched"),
            )
            continue
        if not _is_mobile_app_target(notify_service):
            _LOGGER.warning(
                "K93 ANS: not clearing %s on notify.%s - not recognized as a mobile_app target "
                "(only notify.mobile_app_* services get the clear_notification command)",
                record["id"],
                notify_service,
            )
            continue
        await _send_clear_notification(hass, notify_service, record["id"])


async def async_clear_inactive_live_recipients(
    hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore
) -> None:
    timeout_minutes = entry.options.get(CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES) or 0
    if timeout_minutes <= 0:
        return

    recipient_defs = {r["id"]: r for r in entry.options.get(CONF_RECIPIENTS, [])}
    timeout = timedelta(minutes=timeout_minutes)
    now = dt_util.utcnow()

    for record in store.async_list():
        if not record.get("live_id") or record.get("acknowledged"):
            continue
        if not record.get("interactive_only"):
            continue

        deliveries = record.get("recipients") or {}
        changed = False
        for recipient_id, delivery in deliveries.items():
            notify_service = delivery.get("notify_service")
            if not delivery.get("dispatched") or not notify_service:
                continue
            if not _is_mobile_app_target(notify_service):
                continue

            recipient = recipient_defs.get(recipient_id)
            interactive_entity_id = recipient.get("interactive_entity_id") if recipient else None
            if not interactive_entity_id:
                continue

            state = hass.states.get(interactive_entity_id)
            if state is None or state.state != STATE_OFF:
                continue
            if now - state.last_changed < timeout:
                continue

            _LOGGER.info(
                "K93 ANS clearing live notification %s on notify.%s - %s has been inactive "
                "for over %d minute(s)",
                record["id"],
                notify_service,
                interactive_entity_id,
                timeout_minutes,
            )
            await _send_clear_notification(hass, notify_service, record["id"])
            delivery["dispatched"] = False
            changed = True

        if changed:
            await store.async_update_record(record)


async def async_acknowledge(
    hass: HomeAssistant, store: NotificationStore, notification_id: str, via: str
) -> NotificationRecord | None:
    record = await store.async_acknowledge(notification_id, via)
    if record is None:
        return None
    clear_on_acknowledge = record.get("clear_on_acknowledge", True)
    _LOGGER.warning(
        "K93 ANS acknowledge %s via=%s persistent=%s clear_on_acknowledge=%s",
        notification_id,
        via,
        record.get("persistent"),
        clear_on_acknowledge,
    )
    if record.get("persistent"):
        try:
            persistent_notification.async_dismiss(hass, notification_id)
        except Exception:
            _LOGGER.exception(
                "K93 ANS failed dismissing persistent_notification %s", notification_id
            )
    if clear_on_acknowledge:
        await _clear_mobile_notifications(hass, record)
    async_dispatcher_send(hass, SIGNAL_UPDATED, record)
    return record


def async_register_persistent_notification_listener(
    hass: HomeAssistant, store: NotificationStore
) -> Callable[[], None]:

    @callback
    def _on_update(update_type: UpdateType, notifications: dict[str, Any]) -> None:
        if update_type != UpdateType.REMOVED:
            return
        for notification_id in notifications:
            record = store.async_get(notification_id)
            if record is None or not record.get("persistent") or record.get("acknowledged"):
                continue
            hass.async_create_task(async_acknowledge(hass, store, notification_id, "ha_ui"))

    return async_dispatcher_connect(hass, SIGNAL_PERSISTENT_NOTIFICATIONS_UPDATED, _on_update)


async def async_delete_notifications(
    hass: HomeAssistant, store: NotificationStore, notification_ids: list[str]
) -> list[str]:
    for notification_id in notification_ids:
        record = store.async_get(notification_id)
        if record is None:
            continue
        if record.get("persistent") and not record.get("acknowledged"):
            persistent_notification.async_dismiss(hass, notification_id)
        await _clear_mobile_notifications(hass, record)

    deleted_ids = await store.async_delete(notification_ids)
    if deleted_ids:
        async_dispatcher_send(hass, SIGNAL_DELETED, deleted_ids)
    return deleted_ids
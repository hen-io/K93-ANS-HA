from __future__ import annotations

import uuid

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .config_snapshot import async_restore_config_from_snapshot
from .const import (
    ACK_ACTION_LABELS,
    ACK_ACTION_PREFIX,
    CONF_LANGUAGE,
    DEFAULT_CHANNEL,
    DEFAULT_IMPORTANCE,
    DEFAULT_LANGUAGE,
    DOMAIN,
    EVENT_NOTIFICATION,
    IMPORTANCE_LEVELS,
    MAX_ACTIONS,
)
from .dispatch import async_acknowledge, async_delete_notifications
from .image_capture import async_capture_entity_image
from .models import NotificationRecord
from .store import NotificationStore

SERVICE_SEND_NOTIFICATION = "send_notification"
SERVICE_ACKNOWLEDGE = "acknowledge"
SERVICE_END_LIVE_NOTIFICATION = "end_live_notification"
SERVICE_DELETE_NOTIFICATION = "delete_notification"
SERVICE_CLEAR_HISTORY = "clear_history"
SERVICE_RESTORE_CONFIG_FROM_SNAPSHOT = "restore_config_from_snapshot"

ACKNOWLEDGE_SCHEMA = vol.Schema({vol.Required("notification_id"): cv.string})
END_LIVE_NOTIFICATION_SCHEMA = vol.Schema({vol.Required("live_id"): cv.string})
DELETE_NOTIFICATION_SCHEMA = vol.Schema({vol.Required("notification_id"): cv.string})
CLEAR_HISTORY_SCHEMA = vol.Schema(
    {vol.Optional("include_unacknowledged", default=False): cv.boolean}
)
RESTORE_CONFIG_FROM_SNAPSHOT_SCHEMA = vol.Schema({vol.Required("confirm"): cv.boolean})

ACTION_SCHEMA = vol.Schema(
    {
        vol.Required("action"): cv.string,
        vol.Required("title"): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)

SEND_NOTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required("title"): cv.string,
        vol.Required("message"): cv.string,
        vol.Optional("icon"): cv.string,
        vol.Optional("image"): cv.string,
        vol.Optional("image_entity"): cv.entity_id,
        vol.Optional("channel", default=DEFAULT_CHANNEL): vol.Any(cv.string, [cv.string]),
        vol.Optional("importance", default=DEFAULT_IMPORTANCE): vol.In(IMPORTANCE_LEVELS),
        vol.Optional("actions", default=list): [ACTION_SCHEMA],
        vol.Optional("persistent", default=False): cv.boolean,
        vol.Optional("data", default=dict): dict,
        vol.Optional("target_recipients"): [cv.string],
        vol.Optional("home_only", default=False): cv.boolean,
        vol.Optional("live_id"): cv.string,
        vol.Optional("source"): cv.string,
        vol.Optional("show_in_history", default=True): cv.boolean,
        vol.Optional("dismiss_on_action", default=False): cv.boolean,
        vol.Optional("clear_on_acknowledge", default=True): cv.boolean,
    }
)


def _resolve_language(hass: HomeAssistant, entry: ConfigEntry) -> str:
    configured = entry.options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
    if configured and configured != "auto":
        return configured
    hass_language = (hass.config.language or "").lower()
    return "no" if hass_language.startswith(("nb", "nn", "no")) else "en"


def _build_record(
    data: dict, ack_label: str, existing: dict[str, str] | None
) -> NotificationRecord:
    notification_id = existing["id"] if existing else str(uuid.uuid4())
    created = existing["created"] if existing else dt_util.utcnow().isoformat()
    actions = list(data["actions"])
    raw_channels = data["channel"]
    if isinstance(raw_channels, str):
        raw_channels = [raw_channels]
    channels = [c.strip().lower().replace(" ", "_") for c in raw_channels if c and c.strip()]
    if not channels:
        channels = [DEFAULT_CHANNEL]
    channel = channels[0]
    persistent = data["persistent"]

    has_ack_action = any(a["action"].startswith(ACK_ACTION_PREFIX) for a in actions)
    if persistent and not has_ack_action:
        actions = actions[: MAX_ACTIONS - 1]
        actions.append(
            {"action": f"{ACK_ACTION_PREFIX}{notification_id}", "title": ack_label}
        )
    actions = actions[:MAX_ACTIONS]

    return {
        "id": notification_id,
        "created": created,
        "updated_at": dt_util.utcnow().isoformat() if existing else None,
        "live_id": data.get("live_id"),
        "title": data["title"],
        "message": data["message"],
        "icon": data.get("icon"),
        "image": data.get("image"),
        "image_managed": False,
        "channel": channel,
        "channels": channels,
        "importance": data["importance"],
        "actions": actions,
        "persistent": persistent,
        "data": data["data"],
        "recipients": {},
        "requires_ack": persistent,
        "acknowledged": False,
        "acknowledged_at": None,
        "acknowledged_via": None,
        "dismissed": False,
        "source": data.get("source"),
        "target_recipients": data.get("target_recipients"),
        "home_only": data["home_only"],
        "show_in_history": data["show_in_history"],
        "dismiss_on_action": data["dismiss_on_action"],
        "clear_on_acknowledge": data["clear_on_acknowledge"],
    }


async def async_send_notification(
    hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore, data: dict
) -> None:
    language = _resolve_language(hass, entry)
    ack_label = ACK_ACTION_LABELS.get(language, ACK_ACTION_LABELS["en"])
    live_id = data.get("live_id")
    existing = store.async_resolve_live_notification(live_id) if live_id else None
    record = _build_record(data, ack_label, existing)
    image_entity = data.get("image_entity")
    if image_entity and not record.get("image"):
        await async_capture_entity_image(hass, record, image_entity)
    if live_id:
        store.async_reserve_live_id(live_id, record["id"], record["created"])
    hass.bus.async_fire(EVENT_NOTIFICATION, record)


def async_register_services(hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore) -> None:

    async def handle_send_notification(call: ServiceCall) -> None:
        await async_send_notification(hass, entry, store, dict(call.data))

    async def handle_acknowledge(call: ServiceCall) -> None:
        await async_acknowledge(hass, store, call.data["notification_id"], "service")

    async def handle_end_live_notification(call: ServiceCall) -> None:
        record = store.async_get_by_live_id(call.data["live_id"])
        if record is None:
            return
        await async_acknowledge(hass, store, record["id"], "live_ended")

    async def handle_delete_notification(call: ServiceCall) -> None:
        await async_delete_notifications(hass, store, [call.data["notification_id"]])

    async def handle_clear_history(call: ServiceCall) -> None:
        ids = store.async_history_ids(call.data["include_unacknowledged"])
        await async_delete_notifications(hass, store, ids)

    async def handle_restore_config_from_snapshot(call: ServiceCall) -> None:
        if not call.data["confirm"]:
            raise ServiceValidationError(
                "Set confirm: true to restore the config snapshot - this replaces the entire "
                "current configuration."
            )
        await async_restore_config_from_snapshot(hass, entry, store)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_NOTIFICATION,
        handle_send_notification,
        schema=SEND_NOTIFICATION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACKNOWLEDGE,
        handle_acknowledge,
        schema=ACKNOWLEDGE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_END_LIVE_NOTIFICATION,
        handle_end_live_notification,
        schema=END_LIVE_NOTIFICATION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_NOTIFICATION,
        handle_delete_notification,
        schema=DELETE_NOTIFICATION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_HISTORY,
        handle_clear_history,
        schema=CLEAR_HISTORY_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_CONFIG_FROM_SNAPSHOT,
        handle_restore_config_from_snapshot,
        schema=RESTORE_CONFIG_FROM_SNAPSHOT_SCHEMA,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    hass.services.async_remove(DOMAIN, SERVICE_SEND_NOTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_ACKNOWLEDGE)
    hass.services.async_remove(DOMAIN, SERVICE_END_LIVE_NOTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_NOTIFICATION)
    hass.services.async_remove(DOMAIN, SERVICE_RESTORE_CONFIG_FROM_SNAPSHOT)
    hass.services.async_remove(DOMAIN, SERVICE_CLEAR_HISTORY)

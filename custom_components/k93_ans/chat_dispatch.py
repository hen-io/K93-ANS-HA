from __future__ import annotations

import logging
import uuid
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .chat_store import ChatStore
from .const import (
    CHAT_ALERT_LIVE_ID_PREFIX,
    CONF_CHANNELS,
    CONF_RECIPIENTS,
    IMPORTANCE_LEVELS,
    SIGNAL_CHAT_MESSAGE,
    SIGNAL_CHAT_READ,
    SIGNAL_CHAT_REACTION,
)
from .dispatch import async_acknowledge
from .models import ChatMessage
from .services import SEND_NOTIFICATION_SCHEMA, async_send_notification
from .store import NotificationStore

_LOGGER = logging.getLogger(__name__)

_MESSAGE_PREVIEW_MAX_LEN = 120
_CHAT_ALERT_IMPORTANCE = "normal"


def _importance_rank(level: str) -> int:
    try:
        return IMPORTANCE_LEVELS.index(level)
    except ValueError:
        return IMPORTANCE_LEVELS.index("normal")


async def _resolve_room_members(hass: HomeAssistant, chatroom: dict[str, Any]) -> list[str]:
    if chatroom.get("access_mode") == "selected":
        return list(chatroom.get("allowed_user_ids") or [])
    users = await hass.auth.async_get_users()
    return [u.id for u in users if not u.system_generated]


def _linked_recipient_ids(entry: ConfigEntry, user_id: str) -> list[str]:
    return [
        r["id"]
        for r in entry.options.get(CONF_RECIPIENTS, [])
        if r.get("linked_user_id") == user_id and r.get("enabled", True)
    ]


def _diagnose_recipient_delivery(entry: ConfigEntry, recipient: dict[str, Any]) -> None:
    allowed_channels = recipient.get("allowed_channels") or []
    if allowed_channels and "chat" not in allowed_channels:
        _LOGGER.warning(
            "K93 ANS chat alert: recipient '%s' is linked for chat alerts, but its own Allowed "
            "channels doesn't include 'chat' (currently: %s) - the alert will be silently "
            "filtered out. Add 'chat' to that recipient's Allowed channels, or clear the list "
            "entirely to allow every channel.",
            recipient.get("name", recipient.get("id")),
            allowed_channels,
        )
        return

    channel_defs = {c["key"]: c for c in entry.options.get(CONF_CHANNELS, [])}
    chat_channel = channel_defs.get("chat")
    if chat_channel is not None and not chat_channel.get("enabled", True):
        _LOGGER.warning(
            "K93 ANS chat alert: the 'chat' channel itself is disabled (Manage channels) - no "
            "chat alert can be delivered to any recipient until it's re-enabled."
        )
        return

    per_channel_importance = (recipient.get("channel_importance") or {}).get("chat")
    effective_min = per_channel_importance or recipient.get("min_importance", "low")
    channel_min = chat_channel.get("min_importance", "low") if chat_channel else "low"
    required_rank = max(_importance_rank(effective_min), _importance_rank(channel_min))
    if _importance_rank(_CHAT_ALERT_IMPORTANCE) < required_rank:
        _LOGGER.warning(
            "K93 ANS chat alert: recipient '%s' requires at least '%s' importance to receive "
            "'chat' notifications, but chat alerts are always sent at '%s' - the alert will be "
            "silently filtered out. Lower that recipient's minimum importance, or add a 'chat' "
            "per-channel importance override for it, to '%s' or below.",
            recipient.get("name", recipient.get("id")),
            IMPORTANCE_LEVELS[required_rank],
            _CHAT_ALERT_IMPORTANCE,
            _CHAT_ALERT_IMPORTANCE,
        )


async def async_post_chat_message(
    hass: HomeAssistant,
    entry: ConfigEntry,
    store: NotificationStore,
    chat_store: ChatStore,
    chatroom: dict[str, Any],
    data: dict[str, Any],
) -> ChatMessage:
    message: ChatMessage = {
        "id": data.get("id") or str(uuid.uuid4()),
        "chatroom_id": chatroom["id"],
        "sender_user_id": data.get("sender_user_id"),
        "sender_name": data.get("sender_name"),
        "sender_icon": data.get("sender_icon"),
        "message": data["message"],
        "created": dt_util.utcnow().isoformat(),
        "source": data.get("source"),
        "image": data.get("image"),
    }
    await chat_store.async_add_message(message)

    sender_user_id = message["sender_user_id"]
    if sender_user_id:
        await async_mark_chatroom_read(hass, store, chat_store, chatroom["id"], sender_user_id)

    async_dispatcher_send(
        hass, SIGNAL_CHAT_MESSAGE, {"chatroom_id": chatroom["id"], "message": message}
    )

    if chatroom.get("new_message_alert"):
        await _send_chat_alerts(hass, entry, store, chat_store, chatroom, message)

    return message


async def _send_chat_alerts(
    hass: HomeAssistant,
    entry: ConfigEntry,
    store: NotificationStore,
    chat_store: ChatStore,
    chatroom: dict[str, Any],
    message: ChatMessage,
) -> None:
    sender_user_id = message.get("sender_user_id")
    members = await _resolve_room_members(hass, chatroom)
    preview = message["message"]
    if len(preview) > _MESSAGE_PREVIEW_MAX_LEN:
        preview = preview[: _MESSAGE_PREVIEW_MAX_LEN - 1] + "…"

    _LOGGER.warning(
        "K93 ANS chat alert: chatroom=%s new_message_alert=%s sender=%s members=%s",
        chatroom.get("id"),
        chatroom.get("new_message_alert"),
        sender_user_id,
        members,
    )

    for member_user_id in members:
        if member_user_id == sender_user_id:
            continue
        if chat_store.async_unread_count(chatroom["id"], member_user_id) <= 0:
            _LOGGER.warning(
                "K93 ANS chat alert: member=%s already caught up, skipping", member_user_id
            )
            continue

        recipient_ids = _linked_recipient_ids(entry, member_user_id)
        if not recipient_ids:
            _LOGGER.warning(
                "K93 ANS chat alert: member=%s has no Recipient with a matching 'Linked user' - "
                "skipping, nothing to push to. Set that member's account as the 'Linked user' on "
                "one of their Recipients (Manage recipients) to receive a push.",
                member_user_id,
            )
            continue

        alert_fields: dict[str, Any] = {
            "title": chatroom["name"],
            "message": preview,
            "channel": "chat",
            "target_recipients": recipient_ids,
            "live_id": f"{CHAT_ALERT_LIVE_ID_PREFIX}{chatroom['id']}_{member_user_id}",
            "persistent": False,
            "show_in_history": False,
            "source": f"chat:{chatroom['id']}",
        }
        if chatroom.get("icon"):
            alert_fields["icon"] = chatroom["icon"]
        if chatroom.get("navigate_url"):
            alert_fields["data"] = {"clickAction": chatroom["navigate_url"]}

        _LOGGER.warning(
            "K93 ANS chat alert: chatroom=%s member=%s -> recipients=%s",
            chatroom["id"],
            member_user_id,
            recipient_ids,
        )
        recipient_defs = {r["id"]: r for r in entry.options.get(CONF_RECIPIENTS, [])}
        for recipient_id in recipient_ids:
            recipient = recipient_defs.get(recipient_id)
            if recipient is not None:
                _diagnose_recipient_delivery(entry, recipient)

        alert_data = SEND_NOTIFICATION_SCHEMA(alert_fields)
        await async_send_notification(hass, entry, store, alert_data)


async def async_mark_chatroom_read(
    hass: HomeAssistant,
    store: NotificationStore,
    chat_store: ChatStore,
    chatroom_id: str,
    user_id: str,
) -> None:
    latest = chat_store.async_latest_message(chatroom_id)
    await chat_store.async_mark_read(
        chatroom_id,
        user_id,
        latest["id"] if latest else None,
        dt_util.utcnow().isoformat(),
    )
    async_dispatcher_send(hass, SIGNAL_CHAT_READ, {"chatroom_id": chatroom_id, "user_id": user_id})

    record = store.async_get_by_live_id(f"{CHAT_ALERT_LIVE_ID_PREFIX}{chatroom_id}_{user_id}")
    if record is not None:
        await async_acknowledge(hass, store, record["id"], "chat_read")


def group_reactions(raw: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for row in raw:
        grouped.setdefault(row["emoji"], []).append(row["user_id"])
    return [
        {"emoji": emoji, "count": len(user_ids), "user_ids": user_ids}
        for emoji, user_ids in grouped.items()
    ]


async def async_toggle_reaction(
    hass: HomeAssistant,
    chat_store: ChatStore,
    chatroom_id: str,
    message_id: str,
    user_id: str,
    emoji: str,
) -> list[dict[str, Any]]:
    existing = chat_store.async_get_reactions(message_id)
    already_reacted = any(r["user_id"] == user_id and r["emoji"] == emoji for r in existing)
    if already_reacted:
        await chat_store.async_remove_reaction(message_id, user_id, emoji)
    else:
        await chat_store.async_add_reaction(message_id, user_id, emoji)

    reactions = group_reactions(chat_store.async_get_reactions(message_id))
    async_dispatcher_send(
        hass,
        SIGNAL_CHAT_REACTION,
        {"chatroom_id": chatroom_id, "message_id": message_id, "reactions": reactions},
    )
    return reactions

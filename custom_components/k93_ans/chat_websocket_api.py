from __future__ import annotations

import uuid

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .chat_dispatch import (
    async_mark_chatroom_read,
    async_post_chat_message,
    async_toggle_reaction,
    group_reactions,
)
from .chat_identity import resolve_sender
from .chat_store import ChatStore
from .const import (
    CONF_CHAT_REACTION_EMOJI,
    CONF_CHATROOMS,
    DEFAULT_CHAT_REACTION_EMOJI,
    DOMAIN,
    SIGNAL_CHAT_MESSAGE,
    SIGNAL_CHAT_READ,
    SIGNAL_CHAT_REACTION,
)
from .image_capture import async_save_chat_image
from .store import NotificationStore


def async_register_chat_websocket_api(
    hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore, chat_store: ChatStore
) -> None:

    def _chatrooms() -> list[dict]:
        return entry.options.get(CONF_CHATROOMS, [])

    def _reaction_emoji() -> list[str]:
        return entry.options.get(CONF_CHAT_REACTION_EMOJI) or list(DEFAULT_CHAT_REACTION_EMOJI)

    def _find_chatroom(chatroom_id: str) -> dict | None:
        return next((c for c in _chatrooms() if c["id"] == chatroom_id), None)

    def _room_accessible(chatroom: dict, user_id: str) -> bool:
        if chatroom.get("access_mode") == "selected":
            return user_id in (chatroom.get("allowed_user_ids") or [])
        return True

    def _user_names() -> dict[str, str]:
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
        return entry_data.get("user_names") or {}

    def _serialize_message(message: dict) -> dict:
        return {
            **message,
            "sender": resolve_sender(hass, _user_names(), message),
            "reactions": group_reactions(chat_store.async_get_reactions(message["id"])),
        }

    @websocket_api.websocket_command({vol.Required("type"): "k93_ans/chat/list_rooms"})
    @callback
    def handle_list_rooms(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        user_id = connection.user.id
        rooms = [
            {**room, "unread_count": chat_store.async_unread_count(room["id"], user_id)}
            for room in _chatrooms()
            if room.get("enabled", True) and _room_accessible(room, user_id)
        ]
        connection.send_result(msg["id"], {"chatrooms": rooms, "reaction_emoji": _reaction_emoji()})

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "k93_ans/chat/list_messages",
            vol.Required("chatroom_id"): str,
            vol.Optional("limit"): int,
            vol.Optional("before_id"): str,
        }
    )
    @callback
    def handle_list_messages(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        chatroom = _find_chatroom(msg["chatroom_id"])
        if chatroom is None or not _room_accessible(chatroom, connection.user.id):
            connection.send_error(msg["id"], "access_denied", "No access to this chatroom")
            return
        messages = chat_store.async_list_messages(
            msg["chatroom_id"], limit=msg.get("limit"), before_id=msg.get("before_id")
        )
        names = _user_names()
        read_states = [
            {**rs, "name": names.get(rs["user_id"], "Unknown user")}
            for rs in chat_store.async_room_read_states(msg["chatroom_id"])
        ]
        connection.send_result(
            msg["id"],
            {
                "messages": [_serialize_message(m) for m in messages],
                "read_states": read_states,
                "reaction_emoji": _reaction_emoji(),
            },
        )

    @websocket_api.websocket_command(
        {vol.Required("type"): "k93_ans/chat/subscribe", vol.Required("chatroom_id"): str}
    )
    @callback
    def handle_subscribe(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        chatroom = _find_chatroom(msg["chatroom_id"])
        if chatroom is None or not _room_accessible(chatroom, connection.user.id):
            connection.send_error(msg["id"], "access_denied", "No access to this chatroom")
            return
        chatroom_id = msg["chatroom_id"]

        @callback
        def forward_message(payload: dict) -> None:
            if payload["chatroom_id"] != chatroom_id:
                return
            connection.send_message(
                websocket_api.event_message(
                    msg["id"], {"message": _serialize_message(payload["message"])}
                )
            )

        @callback
        def forward_read(payload: dict) -> None:
            if payload["chatroom_id"] != chatroom_id:
                return
            connection.send_message(
                websocket_api.event_message(msg["id"], {"read": {"user_id": payload["user_id"]}})
            )

        @callback
        def forward_reaction(payload: dict) -> None:
            if payload["chatroom_id"] != chatroom_id:
                return
            connection.send_message(
                websocket_api.event_message(
                    msg["id"],
                    {
                        "reaction": {
                            "message_id": payload["message_id"],
                            "reactions": payload["reactions"],
                        }
                    },
                )
            )

        unsub_message = async_dispatcher_connect(hass_, SIGNAL_CHAT_MESSAGE, forward_message)
        unsub_read = async_dispatcher_connect(hass_, SIGNAL_CHAT_READ, forward_read)
        unsub_reaction = async_dispatcher_connect(hass_, SIGNAL_CHAT_REACTION, forward_reaction)

        @callback
        def unsub() -> None:
            unsub_message()
            unsub_read()
            unsub_reaction()

        connection.subscriptions[msg["id"]] = unsub
        connection.send_result(msg["id"])

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "k93_ans/chat/send",
            vol.Required("chatroom_id"): str,
            vol.Required("message"): str,
            vol.Optional("image_base64"): vol.All(str, vol.Length(max=8_000_000)),
            vol.Optional("image_content_type"): vol.In(
                ["image/jpeg", "image/png", "image/webp", "image/gif"]
            ),
        }
    )
    @websocket_api.async_response
    async def handle_send(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        chatroom = _find_chatroom(msg["chatroom_id"])
        if chatroom is None or not _room_accessible(chatroom, connection.user.id):
            connection.send_error(msg["id"], "access_denied", "No access to this chatroom")
            return
        message_id = str(uuid.uuid4())
        image_url = None
        if msg.get("image_base64"):
            image_url = await async_save_chat_image(
                hass_,
                msg["chatroom_id"],
                message_id,
                msg["image_base64"],
                msg.get("image_content_type") or "image/jpeg",
            )
        message = await async_post_chat_message(
            hass_,
            entry,
            store,
            chat_store,
            chatroom,
            {
                "id": message_id,
                "message": msg["message"],
                "sender_user_id": connection.user.id,
                "source": "card",
                "image": image_url,
            },
        )
        connection.send_result(msg["id"], {"message": _serialize_message(message)})

    @websocket_api.websocket_command(
        {vol.Required("type"): "k93_ans/chat/mark_read", vol.Required("chatroom_id"): str}
    )
    @websocket_api.async_response
    async def handle_mark_read(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        chatroom = _find_chatroom(msg["chatroom_id"])
        if chatroom is None or not _room_accessible(chatroom, connection.user.id):
            connection.send_error(msg["id"], "access_denied", "No access to this chatroom")
            return
        await async_mark_chatroom_read(
            hass_, store, chat_store, msg["chatroom_id"], connection.user.id
        )
        connection.send_result(msg["id"])

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "k93_ans/chat/toggle_reaction",
            vol.Required("chatroom_id"): str,
            vol.Required("message_id"): str,
            vol.Required("emoji"): vol.All(str, vol.Length(min=1, max=32)),
        }
    )
    @websocket_api.async_response
    async def handle_toggle_reaction(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        chatroom = _find_chatroom(msg["chatroom_id"])
        if chatroom is None or not _room_accessible(chatroom, connection.user.id):
            connection.send_error(msg["id"], "access_denied", "No access to this chatroom")
            return
        if not chat_store.async_message_belongs_to_room(msg["chatroom_id"], msg["message_id"]):
            connection.send_error(msg["id"], "not_found", "No such message in this chatroom")
            return
        reactions = await async_toggle_reaction(
            hass_, chat_store, msg["chatroom_id"], msg["message_id"], connection.user.id, msg["emoji"]
        )
        connection.send_result(msg["id"], {"reactions": reactions})

    websocket_api.async_register_command(hass, handle_list_rooms)
    websocket_api.async_register_command(hass, handle_list_messages)
    websocket_api.async_register_command(hass, handle_subscribe)
    websocket_api.async_register_command(hass, handle_send)
    websocket_api.async_register_command(hass, handle_mark_read)
    websocket_api.async_register_command(hass, handle_toggle_reaction)

from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import SIGNAL_DELETED, SIGNAL_UPDATED
from .dispatch import async_acknowledge, async_delete_notifications
from .store import NotificationStore


def async_register_websocket_api(hass: HomeAssistant, store: NotificationStore) -> None:

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "k93_ans/list",
            vol.Optional("include_acknowledged", default=True): bool,
            vol.Optional("limit"): int,
            vol.Optional("channels"): [str],
            vol.Optional("channel_mode", default="include"): vol.In(["include", "exclude"]),
            vol.Optional("min_importance"): str,
        }
    )
    @callback
    def handle_list(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        records = store.async_list(
            include_acknowledged=msg["include_acknowledged"],
            limit=msg.get("limit"),
            channels=msg.get("channels"),
            channel_mode=msg["channel_mode"],
            min_importance=msg.get("min_importance"),
        )
        connection.send_result(msg["id"], {"notifications": records})

    @websocket_api.websocket_command({vol.Required("type"): "k93_ans/subscribe"})
    @callback
    def handle_subscribe(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        @callback
        def forward_updated(record: dict) -> None:
            connection.send_message(
                websocket_api.event_message(msg["id"], {"notification": record})
            )

        @callback
        def forward_deleted(deleted_ids: list[str]) -> None:
            connection.send_message(
                websocket_api.event_message(msg["id"], {"deleted_ids": deleted_ids})
            )

        unsub_updated = async_dispatcher_connect(hass_, SIGNAL_UPDATED, forward_updated)
        unsub_deleted = async_dispatcher_connect(hass_, SIGNAL_DELETED, forward_deleted)

        @callback
        def unsub() -> None:
            unsub_updated()
            unsub_deleted()

        connection.subscriptions[msg["id"]] = unsub
        connection.send_result(msg["id"])

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "k93_ans/acknowledge",
            vol.Required("notification_id"): str,
        }
    )
    @websocket_api.async_response
    async def handle_acknowledge(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        record = await async_acknowledge(hass_, store, msg["notification_id"], "card")
        if record is None:
            connection.send_error(msg["id"], "not_found", "Notification not found")
            return
        connection.send_result(msg["id"], {"notification": record})

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "k93_ans/delete",
            vol.Required("notification_ids"): [str],
        }
    )
    @websocket_api.async_response
    async def handle_delete(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        deleted_ids = await async_delete_notifications(hass_, store, msg["notification_ids"])
        connection.send_result(msg["id"], {"deleted_ids": deleted_ids})

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "k93_ans/clear_history",
            vol.Optional("include_unacknowledged", default=False): bool,
        }
    )
    @websocket_api.async_response
    async def handle_clear_history(
        hass_: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        ids = store.async_history_ids(msg["include_unacknowledged"])
        deleted_ids = await async_delete_notifications(hass_, store, ids)
        connection.send_result(msg["id"], {"deleted_ids": deleted_ids})

    websocket_api.async_register_command(hass, handle_list)
    websocket_api.async_register_command(hass, handle_subscribe)
    websocket_api.async_register_command(hass, handle_acknowledge)
    websocket_api.async_register_command(hass, handle_delete)
    websocket_api.async_register_command(hass, handle_clear_history)

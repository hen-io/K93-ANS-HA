from __future__ import annotations

from typing import Any, TypedDict


class RecipientDelivery(TypedDict):

    notify_service: str
    matched: bool
    dispatched: bool
    dispatch_error: str | None
    dispatched_at: str | None


class NotificationRecord(TypedDict):

    id: str
    created: str
    updated_at: str | None
    live_id: str | None
    title: str
    message: str
    icon: str | None
    image: str | None
    image_managed: bool
    channel: str
    channels: list[str]
    importance: str
    actions: list[dict[str, str]]
    persistent: bool
    data: dict[str, Any]
    recipients: dict[str, RecipientDelivery]
    requires_ack: bool
    acknowledged: bool
    acknowledged_at: str | None
    acknowledged_via: str | None
    dismissed: bool
    source: str | None
    target_recipients: list[str] | None
    home_only: bool
    interactive_only: bool
    show_in_history: bool
    dismiss_on_action: bool
    clear_on_acknowledge: bool


class Recipient(TypedDict):

    id: str
    name: str
    notify_service: str
    min_importance: str
    allowed_channels: list[str]
    channel_importance: dict[str, str]
    enabled: bool
    person_entity_id: str | None
    interactive_entity_id: str | None


class Channel(TypedDict):

    id: str
    key: str
    name: str
    min_importance: str
    enabled: bool
    color: str | None
    retention_days: int | None
    max_records: int | None


class ScheduledNotification(TypedDict):

    id: str
    name: str
    enabled: bool
    cron: str
    title: str
    message: str
    icon: str | None
    channel: str
    importance: str
    persistent: bool
    home_only: bool
    target_recipients: list[str]

from __future__ import annotations

import uuid

DOMAIN = "k93_ans"

EVENT_NOTIFICATION = "k93_ans_notification"
SIGNAL_UPDATED = "k93_ans_updated"
SIGNAL_DELETED = "k93_ans_deleted"
SIGNAL_CHAT_MESSAGE = "k93_ans_chat_message"
SIGNAL_CHAT_READ = "k93_ans_chat_read"
SIGNAL_CHAT_REACTION = "k93_ans_chat_reaction"
CHAT_ALERT_LIVE_ID_PREFIX = "chat_"

LEGACY_STORAGE_KEY = "k93_ans_notifications"

DEFAULT_CHANNEL = "default"

IMPORTANCE_LEVELS = ["low", "normal", "high", "critical"]
DEFAULT_IMPORTANCE = "normal"

BUILTIN_CHANNELS = [
    {"key": "info", "name": "Info", "min_importance": "low"},
    {"key": "alert", "name": "Alert", "min_importance": "high"},
    {"key": "event", "name": "Event", "min_importance": "normal"},
    {"key": "reminder", "name": "Reminder", "min_importance": "normal"},
    {"key": "security", "name": "Security", "min_importance": "high"},
    {"key": "system", "name": "System", "min_importance": "normal"},
    {"key": "chat", "name": "Chat", "min_importance": "normal"},
]

ANDROID_IMPORTANCE_MAP = {"low": "low", "normal": "default", "high": "high", "critical": "max"}
IOS_INTERRUPTION_MAP = {
    "low": "passive",
    "normal": "active",
    "high": "time-sensitive",
    "critical": "critical",
}

ACK_ACTION_PREFIX = "K93_ACK__"
MAX_ACTIONS = 3

CONF_RECIPIENTS = "recipients"
CONF_CHANNELS = "channels"
CONF_SCHEDULED_NOTIFICATIONS = "scheduled_notifications"
CONF_CALENDAR_NOTIFICATIONS = "calendar_notifications"
CONF_CHATROOMS = "chatrooms"
CONF_CHAT_HISTORY_MAX_DAYS = "chat_history_max_days"
CONF_CHAT_HISTORY_MAX_MESSAGES = "chat_history_max_messages"
CONF_CHAT_REACTION_EMOJI = "chat_reaction_emoji"
DEFAULT_ALL_DAY_TIME = "08:00:00"
CONF_HISTORY_RETENTION_DAYS = "history_retention_days"
CONF_HISTORY_MAX_RECORDS = "history_max_records"
CONF_LANGUAGE = "language"
CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES = "live_inactivity_timeout_minutes"
CONF_STORAGE_PATH = "storage_path"

DEFAULT_HISTORY_RETENTION_DAYS = 90
DEFAULT_HISTORY_MAX_RECORDS = 500
DEFAULT_CHAT_HISTORY_MAX_DAYS = 365
DEFAULT_CHAT_HISTORY_MAX_MESSAGES = 200
DEFAULT_CHAT_REACTION_EMOJI = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
DEFAULT_LANGUAGE = "auto"
DEFAULT_LIVE_INACTIVITY_TIMEOUT_MINUTES = 0
DEFAULT_STORAGE_PATH = ""
DEFAULT_STORAGE_DIR_NAME = "K93-Advanced-Notification-System"
CUSTOM_STORAGE_FILENAME = "database.db"
LEGACY_JSON_FILENAME = "database.json"
LEGACY_JSON_FILENAME_ALT = "k93_ans_notifications.json"

CONFIG_SNAPSHOT_FILENAME = "config.json"

IMAGES_WEB_PATH_PREFIX = "/local/"

SUPPORTED_LANGUAGES = ["en", "no"]
ACK_ACTION_LABELS = {"en": "Acknowledge", "no": "Bekreft"}


def default_options() -> dict:
    return {
        CONF_RECIPIENTS: [],
        CONF_SCHEDULED_NOTIFICATIONS: [],
        CONF_CALENDAR_NOTIFICATIONS: [],
        CONF_CHATROOMS: [],
        CONF_CHANNELS: [
            {
                "id": str(uuid.uuid4()),
                "key": channel["key"],
                "name": channel["name"],
                "min_importance": channel["min_importance"],
                "enabled": True,
                "color": None,
                "retention_days": None,
                "max_records": None,
            }
            for channel in BUILTIN_CHANNELS
        ],
        CONF_HISTORY_RETENTION_DAYS: DEFAULT_HISTORY_RETENTION_DAYS,
        CONF_HISTORY_MAX_RECORDS: DEFAULT_HISTORY_MAX_RECORDS,
        CONF_CHAT_HISTORY_MAX_DAYS: DEFAULT_CHAT_HISTORY_MAX_DAYS,
        CONF_CHAT_HISTORY_MAX_MESSAGES: DEFAULT_CHAT_HISTORY_MAX_MESSAGES,
        CONF_CHAT_REACTION_EMOJI: list(DEFAULT_CHAT_REACTION_EMOJI),
        CONF_LANGUAGE: DEFAULT_LANGUAGE,
        CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES: DEFAULT_LIVE_INACTIVITY_TIMEOUT_MINUTES,
        CONF_STORAGE_PATH: DEFAULT_STORAGE_PATH,
    }

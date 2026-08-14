from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from croniter import croniter
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import selector

from .config_snapshot import async_write_config_snapshot
from .const import (
    CONF_CALENDAR_NOTIFICATIONS,
    CONF_CHANNELS,
    CONF_HISTORY_MAX_RECORDS,
    CONF_HISTORY_RETENTION_DAYS,
    CONF_LANGUAGE,
    CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES,
    CONF_RECIPIENTS,
    CONF_SCHEDULED_NOTIFICATIONS,
    CONF_STORAGE_PATH,
    DEFAULT_ALL_DAY_TIME,
    DEFAULT_CHANNEL,
    DOMAIN,
    IMPORTANCE_LEVELS,
    default_options,
)

_LOGGER = logging.getLogger(__name__)

ADD_NEW = "__add_new__"
CHANNEL_IMPORTANCE_FIELD_PREFIX = "importance_for_"


class K93AnsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="K93 ANS", data={}, options=default_options())

        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> K93AnsOptionsFlow:
        return K93AnsOptionsFlow()


class K93AnsOptionsFlow(config_entries.OptionsFlow):

    def __init__(self) -> None:
        self._options: dict[str, Any] | None = None
        self._editing_id: str | None = None

    def _ensure_options(self) -> dict[str, Any]:
        if self._options is None:
            self._options = {**default_options(), **dict(self.config_entry.options)}
        return self._options

    async def _async_save(self) -> None:
        options = self._ensure_options()
        self.hass.config_entries.async_update_entry(self.config_entry, options=options)
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if entry_data is not None:
            await async_write_config_snapshot(self.hass, entry_data["store"], options)


    async def async_step_init(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        self._ensure_options()
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "manage_recipients",
                "manage_channels",
                "manage_scheduled",
                "manage_calendar",
                "advanced",
                "finish",
            ],
        )

    async def async_step_finish(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        return self.async_create_entry(title="", data=self._ensure_options())


    async def async_step_manage_recipients(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        options = self._ensure_options()
        recipients = options[CONF_RECIPIENTS]

        if user_input is not None:
            self._editing_id = None if user_input["recipient"] == ADD_NEW else user_input["recipient"]
            return await self.async_step_edit_recipient()

        if not recipients:
            self._editing_id = None
            return await self.async_step_edit_recipient()

        choices = [{"value": r["id"], "label": r["name"]} for r in recipients]
        choices.append({"value": ADD_NEW, "label": "Add new recipient"})

        return self.async_show_form(
            step_id="manage_recipients",
            data_schema=vol.Schema(
                {
                    vol.Required("recipient"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=choices, mode="dropdown")
                    )
                }
            ),
        )

    async def async_step_edit_recipient(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        options = self._ensure_options()
        recipients = options[CONF_RECIPIENTS]
        existing = next((r for r in recipients if r["id"] == self._editing_id), None)

        if user_input is not None:
            if user_input.get("remove") and existing is not None:
                options[CONF_RECIPIENTS] = [r for r in recipients if r["id"] != existing["id"]]
                await self._async_save()
                return await self.async_step_init()

            channel_importance = {
                key[len(CHANNEL_IMPORTANCE_FIELD_PREFIX) :]: value
                for key, value in user_input.items()
                if key.startswith(CHANNEL_IMPORTANCE_FIELD_PREFIX) and value
            }
            new_allowed_channels = user_input.get("allowed_channels", [])
            recipient_id = existing["id"] if existing else str(uuid.uuid4())
            recipient = {
                "id": recipient_id,
                "name": user_input["name"],
                "notify_service": user_input["notify_service"],
                "person_entity_id": user_input.get("person_entity_id") or None,
                "interactive_entity_id": user_input.get("interactive_entity_id") or None,
                "min_importance": user_input["min_importance"],
                "allowed_channels": new_allowed_channels,
                "channel_importance": channel_importance,
                "enabled": user_input["enabled"],
            }
            if existing:
                options[CONF_RECIPIENTS] = [
                    recipient if r["id"] == existing["id"] else r for r in recipients
                ]
            else:
                options[CONF_RECIPIENTS] = [*recipients, recipient]

            await self._async_save()

            previous_allowed_channels = set(existing.get("allowed_channels", [])) if existing else set()
            if set(new_allowed_channels) != previous_allowed_channels:
                self._editing_id = recipient_id
                return await self.async_step_edit_recipient()

            return await self.async_step_init()

        notify_services = sorted(self.hass.services.async_services().get("notify", {}).keys())
        channel_choices = [c["key"] for c in options[CONF_CHANNELS]]

        notify_service_key = (
            vol.Required("notify_service", default=existing["notify_service"])
            if existing
            else vol.Required("notify_service")
        )

        schema_dict: dict[Any, Any] = {
            vol.Required("name", default=existing["name"] if existing else ""): selector.TextSelector(),
            notify_service_key: selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=notify_services, custom_value=True, mode="dropdown"
                )
            ),
            vol.Optional(
                "person_entity_id",
                default=(existing.get("person_entity_id") if existing else None),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="person")),
            vol.Optional(
                "interactive_entity_id",
                default=(existing.get("interactive_entity_id") if existing else None),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
            vol.Optional(
                "min_importance",
                default=existing["min_importance"] if existing else "normal",
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=IMPORTANCE_LEVELS)
            ),
            vol.Optional(
                "allowed_channels",
                default=existing["allowed_channels"] if existing else [],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=channel_choices, multiple=True, custom_value=True
                )
            ),
            vol.Optional(
                "enabled", default=existing["enabled"] if existing else True
            ): selector.BooleanSelector(),
        }

        channel_importance = existing.get("channel_importance", {}) if existing else {}
        importance_override_options = [{"value": "", "label": "(use recipient default)"}] + [
            {"value": level, "label": level} for level in IMPORTANCE_LEVELS
        ]
        for channel_key in (existing.get("allowed_channels", []) if existing else []):
            schema_dict[
                vol.Optional(
                    f"{CHANNEL_IMPORTANCE_FIELD_PREFIX}{channel_key}",
                    default=channel_importance.get(channel_key, ""),
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=importance_override_options)
            )

        if existing:
            schema_dict[vol.Optional("remove", default=False)] = selector.BooleanSelector()

        schema = vol.Schema(schema_dict)

        return self.async_show_form(step_id="edit_recipient", data_schema=schema)


    async def async_step_manage_channels(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        options = self._ensure_options()
        channels = options[CONF_CHANNELS]

        if user_input is not None:
            self._editing_id = None if user_input["channel"] == ADD_NEW else user_input["channel"]
            return await self.async_step_edit_channel()

        if not channels:
            self._editing_id = None
            return await self.async_step_edit_channel()

        choices = [{"value": c["id"], "label": c["name"]} for c in channels]
        choices.append({"value": ADD_NEW, "label": "Add new channel"})

        return self.async_show_form(
            step_id="manage_channels",
            data_schema=vol.Schema(
                {
                    vol.Required("channel"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=choices, mode="dropdown")
                    )
                }
            ),
        )

    async def async_step_edit_channel(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        options = self._ensure_options()
        channels = options[CONF_CHANNELS]
        existing = next((c for c in channels if c["id"] == self._editing_id), None)

        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                if user_input.get("remove") and existing is not None:
                    options[CONF_CHANNELS] = [c for c in channels if c["id"] != existing["id"]]
                    await self._async_save()
                    return await self.async_step_init()

                key = user_input["key"].strip().lower().replace(" ", "_")
                duplicate = any(
                    c["key"] == key and (existing is None or c["id"] != existing["id"])
                    for c in channels
                )
                if duplicate:
                    errors["key"] = "duplicate_key"
                else:
                    retention_days = user_input.get("retention_days")
                    max_records = user_input.get("max_records")
                    channel = {
                        "id": existing["id"] if existing else str(uuid.uuid4()),
                        "key": key,
                        "name": user_input["name"],
                        "min_importance": user_input["min_importance"],
                        "enabled": user_input["enabled"],
                        "color": user_input.get("color") or None,
                        "retention_days": int(retention_days)
                        if retention_days not in (None, "")
                        else None,
                        "max_records": int(max_records) if max_records not in (None, "") else None,
                    }
                    if existing:
                        options[CONF_CHANNELS] = [
                            channel if c["id"] == existing["id"] else c for c in channels
                        ]
                    else:
                        options[CONF_CHANNELS] = [*channels, channel]

                    await self._async_save()
                    return await self.async_step_init()
            except Exception:
                _LOGGER.exception(
                    "K93 ANS: failed saving channel (existing=%s, user_input=%s)",
                    existing,
                    user_input,
                )
                errors["base"] = "unknown"

        schema = vol.Schema(
            {
                vol.Required("key", default=existing["key"] if existing else ""): selector.TextSelector(),
                vol.Required("name", default=existing["name"] if existing else ""): selector.TextSelector(),
                vol.Optional(
                    "min_importance",
                    default=existing["min_importance"] if existing else "low",
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=IMPORTANCE_LEVELS)
                ),
                vol.Optional(
                    "enabled", default=existing["enabled"] if existing else True
                ): selector.BooleanSelector(),
                vol.Optional(
                    "color",
                    default="",
                    description={"suggested_value": (existing.get("color") if existing else None) or ""},
                ): selector.TextSelector(),
                vol.Optional(
                    "retention_days",
                    description={
                        "suggested_value": existing.get("retention_days") if existing else None
                    },
                ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, mode="box")),
                vol.Optional(
                    "max_records",
                    description={
                        "suggested_value": existing.get("max_records") if existing else None
                    },
                ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, mode="box")),
            }
        )
        if existing:
            schema = schema.extend(
                {vol.Optional("remove", default=False): selector.BooleanSelector()}
            )

        return self.async_show_form(step_id="edit_channel", data_schema=schema, errors=errors)


    async def async_step_manage_scheduled(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        options = self._ensure_options()
        scheduled = options[CONF_SCHEDULED_NOTIFICATIONS]

        if user_input is not None:
            self._editing_id = (
                None if user_input["scheduled"] == ADD_NEW else user_input["scheduled"]
            )
            return await self.async_step_edit_scheduled()

        if not scheduled:
            self._editing_id = None
            return await self.async_step_edit_scheduled()

        choices = [{"value": s["id"], "label": s["name"]} for s in scheduled]
        choices.append({"value": ADD_NEW, "label": "Add new scheduled notification"})

        return self.async_show_form(
            step_id="manage_scheduled",
            data_schema=vol.Schema(
                {
                    vol.Required("scheduled"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=choices, mode="dropdown")
                    )
                }
            ),
        )

    async def async_step_edit_scheduled(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        options = self._ensure_options()
        scheduled_list = options[CONF_SCHEDULED_NOTIFICATIONS]
        existing = next((s for s in scheduled_list if s["id"] == self._editing_id), None)

        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("remove") and existing is not None:
                options[CONF_SCHEDULED_NOTIFICATIONS] = [
                    s for s in scheduled_list if s["id"] != existing["id"]
                ]
                await self._async_save()
                return await self.async_step_init()

            cron = user_input["cron"].strip()
            if not croniter.is_valid(cron):
                errors["cron"] = "invalid_cron"
            else:
                raw_channel = user_input.get("channel") or DEFAULT_CHANNEL
                channel = raw_channel.strip().lower().replace(" ", "_")
                item = {
                    "id": existing["id"] if existing else str(uuid.uuid4()),
                    "name": user_input["name"],
                    "enabled": user_input["enabled"],
                    "cron": cron,
                    "title": user_input["title"],
                    "message": user_input["message"],
                    "icon": user_input.get("icon") or None,
                    "channel": channel,
                    "importance": user_input["importance"],
                    "persistent": user_input["persistent"],
                    "home_only": user_input["home_only"],
                    "target_recipients": user_input.get("target_recipients", []),
                }
                if existing:
                    options[CONF_SCHEDULED_NOTIFICATIONS] = [
                        item if s["id"] == existing["id"] else s for s in scheduled_list
                    ]
                else:
                    options[CONF_SCHEDULED_NOTIFICATIONS] = [*scheduled_list, item]

                await self._async_save()
                return await self.async_step_init()

        channel_choices = [c["key"] for c in options[CONF_CHANNELS]]
        recipient_choices = [r["name"] for r in options[CONF_RECIPIENTS]]

        schema_dict: dict[Any, Any] = {
            vol.Required("name", default=existing["name"] if existing else ""): selector.TextSelector(),
            vol.Required(
                "cron", default=existing["cron"] if existing else "0 8 * * *"
            ): selector.TextSelector(),
            vol.Optional(
                "enabled", default=existing["enabled"] if existing else True
            ): selector.BooleanSelector(),
            vol.Required("title", default=existing["title"] if existing else ""): selector.TextSelector(),
            vol.Required(
                "message", default=existing["message"] if existing else ""
            ): selector.TextSelector(),
            vol.Optional(
                "icon",
                default="",
                description={"suggested_value": (existing.get("icon") if existing else None) or ""},
            ): selector.TextSelector(),
            vol.Optional(
                "channel",
                default=existing.get("channel", DEFAULT_CHANNEL) if existing else DEFAULT_CHANNEL,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=channel_choices, custom_value=True)
            ),
            vol.Optional(
                "importance",
                default=existing["importance"] if existing else "normal",
            ): selector.SelectSelector(selector.SelectSelectorConfig(options=IMPORTANCE_LEVELS)),
            vol.Optional(
                "persistent", default=existing["persistent"] if existing else False
            ): selector.BooleanSelector(),
            vol.Optional(
                "home_only", default=existing["home_only"] if existing else False
            ): selector.BooleanSelector(),
            vol.Optional(
                "target_recipients",
                default=existing.get("target_recipients", []) if existing else [],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=recipient_choices, multiple=True, custom_value=True
                )
            ),
        }
        if existing:
            schema_dict[vol.Optional("remove", default=False)] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="edit_scheduled", data_schema=vol.Schema(schema_dict), errors=errors
        )


    async def async_step_manage_calendar(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        options = self._ensure_options()
        calendar_notifications = options[CONF_CALENDAR_NOTIFICATIONS]

        if user_input is not None:
            self._editing_id = (
                None if user_input["calendar_notification"] == ADD_NEW else user_input["calendar_notification"]
            )
            return await self.async_step_edit_calendar()

        if not calendar_notifications:
            self._editing_id = None
            return await self.async_step_edit_calendar()

        choices = [{"value": c["id"], "label": c["name"]} for c in calendar_notifications]
        choices.append({"value": ADD_NEW, "label": "Add new calendar notification"})

        return self.async_show_form(
            step_id="manage_calendar",
            data_schema=vol.Schema(
                {
                    vol.Required("calendar_notification"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=choices, mode="dropdown")
                    )
                }
            ),
        )

    async def async_step_edit_calendar(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        options = self._ensure_options()
        calendar_list = options[CONF_CALENDAR_NOTIFICATIONS]
        existing = next((c for c in calendar_list if c["id"] == self._editing_id), None)

        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("remove") and existing is not None:
                options[CONF_CALENDAR_NOTIFICATIONS] = [
                    c for c in calendar_list if c["id"] != existing["id"]
                ]
                await self._async_save()
                return await self.async_step_init()

            raw_channel = user_input.get("channel") or DEFAULT_CHANNEL
            channel = raw_channel.strip().lower().replace(" ", "_")
            item = {
                "id": existing["id"] if existing else str(uuid.uuid4()),
                "name": user_input["name"],
                "enabled": user_input["enabled"],
                "calendar_entity": user_input["calendar_entity"],
                "all_day_time": user_input.get("all_day_time") or DEFAULT_ALL_DAY_TIME,
                "title": user_input.get("title") or None,
                "message": user_input.get("message") or None,
                "icon": user_input.get("icon") or None,
                "channel": channel,
                "importance": user_input["importance"],
                "persistent": user_input["persistent"],
                "home_only": user_input["home_only"],
                "target_recipients": user_input.get("target_recipients", []),
            }
            if existing:
                options[CONF_CALENDAR_NOTIFICATIONS] = [
                    item if c["id"] == existing["id"] else c for c in calendar_list
                ]
            else:
                options[CONF_CALENDAR_NOTIFICATIONS] = [*calendar_list, item]

            await self._async_save()
            return await self.async_step_init()

        channel_choices = [c["key"] for c in options[CONF_CHANNELS]]
        recipient_choices = [r["name"] for r in options[CONF_RECIPIENTS]]

        calendar_entity_key = (
            vol.Required("calendar_entity", default=existing["calendar_entity"])
            if existing
            else vol.Required("calendar_entity")
        )

        schema_dict: dict[Any, Any] = {
            vol.Required("name", default=existing["name"] if existing else ""): selector.TextSelector(),
            calendar_entity_key: selector.EntitySelector(selector.EntitySelectorConfig(domain="calendar")),
            vol.Optional(
                "enabled", default=existing["enabled"] if existing else True
            ): selector.BooleanSelector(),
            vol.Optional(
                "all_day_time",
                default=existing.get("all_day_time", DEFAULT_ALL_DAY_TIME)
                if existing
                else DEFAULT_ALL_DAY_TIME,
            ): selector.TimeSelector(),
            vol.Optional(
                "title",
                default="",
                description={"suggested_value": (existing.get("title") if existing else None) or ""},
            ): selector.TextSelector(),
            vol.Optional(
                "message",
                default="",
                description={"suggested_value": (existing.get("message") if existing else None) or ""},
            ): selector.TextSelector(),
            vol.Optional(
                "icon",
                default="",
                description={"suggested_value": (existing.get("icon") if existing else None) or ""},
            ): selector.TextSelector(),
            vol.Optional(
                "channel",
                default=existing.get("channel", DEFAULT_CHANNEL) if existing else DEFAULT_CHANNEL,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=channel_choices, custom_value=True)
            ),
            vol.Optional(
                "importance",
                default=existing["importance"] if existing else "normal",
            ): selector.SelectSelector(selector.SelectSelectorConfig(options=IMPORTANCE_LEVELS)),
            vol.Optional(
                "persistent", default=existing["persistent"] if existing else False
            ): selector.BooleanSelector(),
            vol.Optional(
                "home_only", default=existing["home_only"] if existing else False
            ): selector.BooleanSelector(),
            vol.Optional(
                "target_recipients",
                default=existing.get("target_recipients", []) if existing else [],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=recipient_choices, multiple=True, custom_value=True
                )
            ),
        }
        if existing:
            schema_dict[vol.Optional("remove", default=False)] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="edit_calendar", data_schema=vol.Schema(schema_dict), errors=errors
        )


    async def async_step_advanced(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        options = self._ensure_options()

        if user_input is not None:
            options[CONF_HISTORY_RETENTION_DAYS] = user_input[CONF_HISTORY_RETENTION_DAYS]
            options[CONF_HISTORY_MAX_RECORDS] = user_input[CONF_HISTORY_MAX_RECORDS]
            options[CONF_LANGUAGE] = user_input[CONF_LANGUAGE]
            options[CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES] = user_input[
                CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES
            ]
            options[CONF_STORAGE_PATH] = user_input.get(CONF_STORAGE_PATH, "").strip()
            await self._async_save()
            return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_HISTORY_RETENTION_DAYS,
                    default=options[CONF_HISTORY_RETENTION_DAYS],
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, mode="box")
                ),
                vol.Optional(
                    CONF_HISTORY_MAX_RECORDS,
                    default=options[CONF_HISTORY_MAX_RECORDS],
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, mode="box")
                ),
                vol.Optional(
                    CONF_LANGUAGE,
                    default=options[CONF_LANGUAGE],
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "auto", "label": "Automatic (match Home Assistant)"},
                            {"value": "en", "label": "English"},
                            {"value": "no", "label": "Norsk"},
                        ]
                    )
                ),
                vol.Optional(
                    CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES,
                    default=options[CONF_LIVE_INACTIVITY_TIMEOUT_MINUTES],
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, mode="box")
                ),
                vol.Optional(
                    CONF_STORAGE_PATH,
                    default="",
                    description={"suggested_value": options.get(CONF_STORAGE_PATH, "")},
                ): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="advanced", data_schema=schema)

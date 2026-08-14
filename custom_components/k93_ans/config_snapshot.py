from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import CONFIG_SNAPSHOT_FILENAME, default_options
from .store import NotificationStore

_LOGGER = logging.getLogger(__name__)


def _write_snapshot(path: Path, options: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(options, indent=2, ensure_ascii=False), encoding="utf-8")


async def async_write_config_snapshot(
    hass: HomeAssistant, store: NotificationStore, options: Mapping[str, Any]
) -> None:
    path = store.storage_dir / CONFIG_SNAPSHOT_FILENAME
    try:
        await hass.async_add_executor_job(_write_snapshot, path, dict(options))
    except OSError:
        _LOGGER.exception("K93 ANS failed writing config snapshot to %s", path)


async def async_restore_config_from_snapshot(
    hass: HomeAssistant, entry: ConfigEntry, store: NotificationStore
) -> None:
    path = store.storage_dir / CONFIG_SNAPSHOT_FILENAME
    try:
        raw = await hass.async_add_executor_job(path.read_text, "utf-8")
    except OSError as err:
        raise ServiceValidationError(f"No config snapshot found at {path}") from err
    try:
        restored = json.loads(raw)
    except ValueError as err:
        raise ServiceValidationError(f"Config snapshot at {path} is not valid JSON: {err}") from err
    if not isinstance(restored, dict):
        raise ServiceValidationError(f"Config snapshot at {path} is not a JSON object")

    options = {**default_options(), **restored}
    hass.config_entries.async_update_entry(entry, options=options)

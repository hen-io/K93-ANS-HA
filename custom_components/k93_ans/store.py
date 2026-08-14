from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CUSTOM_STORAGE_FILENAME,
    DEFAULT_STORAGE_DIR_NAME,
    IMPORTANCE_LEVELS,
    LEGACY_JSON_FILENAME,
    LEGACY_JSON_FILENAME_ALT,
    LEGACY_STORAGE_KEY,
)
from .models import NotificationRecord

_LOGGER = logging.getLogger(__name__)

_LEGACY_JSON_FILENAMES = (LEGACY_JSON_FILENAME, LEGACY_JSON_FILENAME_ALT)


def _importance_rank(level: str) -> int:
    try:
        return IMPORTANCE_LEVELS.index(level)
    except ValueError:
        return IMPORTANCE_LEVELS.index("normal")


def _resolve_storage_dir(hass: HomeAssistant, storage_path: str | None) -> Path:
    default_dir = Path(hass.config.path(DEFAULT_STORAGE_DIR_NAME))
    if not storage_path:
        return default_dir

    directory = Path(storage_path)
    if not directory.is_absolute():
        directory = Path(hass.config.path(storage_path))

    if directory.exists() and not directory.is_dir():
        _LOGGER.error(
            "K93 ANS: storage_path '%s' resolves to %s, which already exists as a file (not a "
            "directory) - falling back to %s. Change storage_path under Advanced to a plain "
            "folder path.",
            storage_path,
            directory,
            default_dir,
        )
        return default_dir

    return directory


def _migrate_storage_dir(hass: HomeAssistant, effective_dir: Path) -> None:
    db_file = effective_dir / CUSTOM_STORAGE_FILENAME
    if db_file.exists() or any((effective_dir / name).exists() for name in _LEGACY_JSON_FILENAMES):
        return

    default_dir = Path(hass.config.path(DEFAULT_STORAGE_DIR_NAME))
    if effective_dir == default_dir:
        return
    default_db = default_dir / CUSTOM_STORAGE_FILENAME
    default_has_json = any((default_dir / name).exists() for name in _LEGACY_JSON_FILENAMES)
    if not (default_db.exists() or default_has_json):
        return

    try:
        effective_dir.mkdir(parents=True, exist_ok=True)
        for item in default_dir.iterdir():
            item.rename(effective_dir / item.name)
        try:
            default_dir.rmdir()
        except OSError:
            pass
        _LOGGER.warning(
            "K93 ANS migrated notification history from %s to %s", default_dir, effective_dir
        )
    except OSError:
        _LOGGER.exception("K93 ANS failed migrating notification history to %s", effective_dir)


def _extract_notifications(data: Any) -> list[NotificationRecord]:
    if not isinstance(data, dict):
        return []
    if "notifications" in data:
        return data.get("notifications") or []
    inner = data.get("data")
    if isinstance(inner, dict):
        return inner.get("notifications") or []
    return []


def _candidate_legacy_paths(
    hass: HomeAssistant, effective_dir: Path, *, include_migrated_backups: bool
) -> list[Path]:
    dirs = [effective_dir]
    default_dir = Path(hass.config.path(DEFAULT_STORAGE_DIR_NAME))
    if default_dir != effective_dir:
        dirs.append(default_dir)

    paths: list[Path] = []
    for directory in dirs:
        for name in _LEGACY_JSON_FILENAMES:
            paths.append(directory / name)
            if include_migrated_backups:
                paths.append(directory / f"{name}.migrated")
    ancient_file = Path(hass.config.path(".storage", LEGACY_STORAGE_KEY))
    paths.append(ancient_file)
    if include_migrated_backups:
        paths.append(ancient_file.with_name(ancient_file.name + ".migrated"))
    return paths


def _import_legacy_json(
    hass: HomeAssistant, effective_dir: Path, *, include_migrated_backups: bool = True
) -> list[NotificationRecord]:
    collected: dict[str, NotificationRecord] = {}
    for legacy_path in _candidate_legacy_paths(
        hass, effective_dir, include_migrated_backups=include_migrated_backups
    ):
        if not legacy_path.exists():
            continue

        try:
            with legacy_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            records = _extract_notifications(data)
        except (OSError, ValueError):
            _LOGGER.exception("K93 ANS failed reading legacy history from %s", legacy_path)
            continue
        if not records:
            continue

        for record in records:
            collected.setdefault(record["id"], record)

        if not legacy_path.name.endswith(".migrated"):
            try:
                legacy_path.rename(legacy_path.with_name(legacy_path.name + ".migrated"))
            except OSError:
                _LOGGER.exception(
                    "K93 ANS failed renaming legacy history file %s after migrating it",
                    legacy_path,
                )

        _LOGGER.warning(
            "K93 ANS migrated %d notification(s) from %s into the new SQLite database",
            len(records),
            legacy_path,
        )

    return list(collected.values())


class NotificationStore:

    def __init__(self, hass: HomeAssistant, storage_path: str | None = None) -> None:
        self._hass = hass
        self._dir = _resolve_storage_dir(hass, storage_path)
        self._db_path = self._dir / CUSTOM_STORAGE_FILENAME
        self._notifications: list[NotificationRecord] = []
        self._pending_live: dict[str, tuple[str, str]] = {}

    @property
    def storage_dir(self) -> Path:
        return self._dir


    def _connect(self) -> sqlite3.Connection:
        self._dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                live_id TEXT,
                channel TEXT NOT NULL,
                created TEXT NOT NULL,
                acknowledged INTEGER NOT NULL,
                record_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_k93_created ON notifications(created)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_k93_live_id ON notifications(live_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_k93_channel ON notifications(channel)")
        return conn

    def _load_all(self) -> list[NotificationRecord]:
        _migrate_storage_dir(self._hass, self._dir)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT record_json FROM notifications ORDER BY created DESC"
            ).fetchall()
        finally:
            conn.close()
        records: list[NotificationRecord] = [json.loads(row[0]) for row in rows]

        imported = _import_legacy_json(self._hass, self._dir, include_migrated_backups=not records)
        if imported:
            for record in imported:
                self._upsert(record)
            existing_ids = {record["id"] for record in records}
            records = records + [record for record in imported if record["id"] not in existing_ids]
            records.sort(key=lambda r: r["created"], reverse=True)

        return records

    def _upsert(self, record: NotificationRecord) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO notifications (id, live_id, channel, created, acknowledged, record_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    live_id = excluded.live_id,
                    channel = excluded.channel,
                    created = excluded.created,
                    acknowledged = excluded.acknowledged,
                    record_json = excluded.record_json
                """,
                (
                    record["id"],
                    record.get("live_id"),
                    record["channel"],
                    record["created"],
                    int(bool(record.get("acknowledged"))),
                    json.dumps(record),
                ),
            )
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception(
                "K93 ANS failed writing notification %s to the database", record.get("id")
            )
        finally:
            conn.close()

    def _delete_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        conn = self._connect()
        try:
            conn.executemany("DELETE FROM notifications WHERE id = ?", [(i,) for i in ids])
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception("K93 ANS failed deleting notifications from the database")
        finally:
            conn.close()


    async def async_load(self) -> None:
        self._notifications = await self._hass.async_add_executor_job(self._load_all)

    def file_size_bytes(self) -> int:
        try:
            return self._db_path.stat().st_size
        except OSError:
            return 0

    async def async_add(self, record: NotificationRecord) -> None:
        live_id = record.get("live_id")
        if live_id and self._pending_live.get(live_id, (None, None))[0] == record["id"]:
            del self._pending_live[live_id]
        self._notifications = [r for r in self._notifications if r["id"] != record["id"]]
        self._notifications.insert(0, record)
        await self._hass.async_add_executor_job(self._upsert, record)

    async def async_update_record(self, record: NotificationRecord) -> None:
        await self._hass.async_add_executor_job(self._upsert, record)

    def async_get(self, notification_id: str) -> NotificationRecord | None:
        for record in self._notifications:
            if record["id"] == notification_id:
                return record
        return None

    def async_get_by_live_id(self, live_id: str) -> NotificationRecord | None:
        for record in self._notifications:
            if record.get("live_id") == live_id and not record["acknowledged"]:
                return record
        return None

    def async_resolve_live_notification(self, live_id: str) -> dict[str, str] | None:
        existing = self.async_get_by_live_id(live_id)
        if existing:
            return {"id": existing["id"], "created": existing["created"]}
        pending = self._pending_live.get(live_id)
        if pending:
            return {"id": pending[0], "created": pending[1]}
        return None

    def async_reserve_live_id(self, live_id: str, notification_id: str, created: str) -> None:
        self._pending_live[live_id] = (notification_id, created)

    def async_list(
        self,
        include_acknowledged: bool = True,
        limit: int | None = None,
        channels: list[str] | None = None,
        channel_mode: str = "include",
        min_importance: str | None = None,
    ) -> list[NotificationRecord]:
        records = self._notifications
        if not include_acknowledged:
            records = [r for r in records if not r["acknowledged"]]
        if channels:
            normalized = {c.strip().lower() for c in channels}

            def _record_channels(r: NotificationRecord) -> set[str]:
                return {c.lower() for c in (r.get("channels") or [r["channel"]])}

            if channel_mode == "exclude":
                records = [r for r in records if not (_record_channels(r) & normalized)]
            else:
                records = [r for r in records if _record_channels(r) & normalized]
        if min_importance:
            min_rank = _importance_rank(min_importance)
            records = [r for r in records if _importance_rank(r["importance"]) >= min_rank]
        if limit is not None:
            records = records[:limit]
        return records

    async def async_acknowledge(
        self, notification_id: str, via: str
    ) -> NotificationRecord | None:
        record = self.async_get(notification_id)
        if record is None:
            return None
        record["acknowledged"] = True
        record["acknowledged_at"] = dt_util.utcnow().isoformat()
        record["acknowledged_via"] = via
        await self._hass.async_add_executor_job(self._upsert, record)
        return record

    async def async_delete(self, notification_ids: list[str]) -> list[str]:
        id_set = set(notification_ids)
        deleted = [r["id"] for r in self._notifications if r["id"] in id_set]
        if deleted:
            self._notifications = [r for r in self._notifications if r["id"] not in id_set]
            await self._hass.async_add_executor_job(self._delete_ids, deleted)
        return deleted

    def async_history_ids(self, include_unacknowledged: bool = False) -> list[str]:
        if include_unacknowledged:
            return [r["id"] for r in self._notifications]
        return [r["id"] for r in self._notifications if not r["requires_ack"] or r["acknowledged"]]

    async def async_prune(
        self,
        channel_defs: list[dict[str, Any]],
        default_max_records: int,
        default_retention_days: int,
    ) -> None:
        channel_lookup = {c["key"]: c for c in channel_defs}
        now = dt_util.utcnow()

        def _retention_days(key: str) -> int:
            channel = channel_lookup.get(key)
            override = channel.get("retention_days") if channel else None
            return override if override else default_retention_days

        def _max_records(key: str) -> int:
            channel = channel_lookup.get(key)
            override = channel.get("max_records") if channel else None
            return override if override else default_max_records

        kept_by_age = [
            r
            for r in self._notifications
            if dt_util.parse_datetime(r["created"]) >= now - timedelta(days=_retention_days(r["channel"]))
        ]

        counts: dict[str, int] = {}
        kept: list[NotificationRecord] = []
        for record in kept_by_age:
            key = record["channel"]
            if counts.get(key, 0) >= _max_records(key):
                continue
            counts[key] = counts.get(key, 0) + 1
            kept.append(record)

        if len(kept) != len(self._notifications):
            kept_ids = {r["id"] for r in kept}
            removed_ids = [r["id"] for r in self._notifications if r["id"] not in kept_ids]
            self._notifications = kept
            await self._hass.async_add_executor_job(self._delete_ids, removed_ids)

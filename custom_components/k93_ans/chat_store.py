from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CUSTOM_STORAGE_FILENAME
from .models import ChatMessage

_LOGGER = logging.getLogger(__name__)


class ChatStore:

    def __init__(self, hass: HomeAssistant, storage_dir: Path) -> None:
        self._hass = hass
        self._dir = storage_dir
        self._db_path = storage_dir / CUSTOM_STORAGE_FILENAME
        self._messages: dict[str, list[ChatMessage]] = {}
        self._reads: dict[tuple[str, str], dict[str, str | None]] = {}
        self._reactions: dict[str, list[dict[str, str]]] = {}


    def _connect(self) -> sqlite3.Connection:
        self._dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                chatroom_id TEXT NOT NULL,
                sender_user_id TEXT,
                created TEXT NOT NULL,
                message_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_k93_chat_chatroom_created "
            "ON chat_messages(chatroom_id, created)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_reads (
                chatroom_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                last_read_message_id TEXT,
                last_read_at TEXT NOT NULL,
                PRIMARY KEY (chatroom_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_reactions (
                message_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                emoji TEXT NOT NULL,
                created TEXT NOT NULL,
                PRIMARY KEY (message_id, user_id, emoji)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_k93_chat_reactions_message ON chat_reactions(message_id)"
        )
        return conn

    def _load_all(self) -> tuple[
        dict[str, list[ChatMessage]],
        dict[tuple[str, str], dict[str, str | None]],
        dict[str, list[dict[str, str]]],
    ]:
        conn = self._connect()
        try:
            message_rows = conn.execute(
                "SELECT message_json FROM chat_messages ORDER BY created DESC"
            ).fetchall()
            read_rows = conn.execute(
                "SELECT chatroom_id, user_id, last_read_message_id, last_read_at FROM chat_reads"
            ).fetchall()
            reaction_rows = conn.execute(
                "SELECT message_id, user_id, emoji FROM chat_reactions"
            ).fetchall()
        finally:
            conn.close()

        messages_by_room: dict[str, list[ChatMessage]] = {}
        for (blob,) in message_rows:
            message: ChatMessage = json.loads(blob)
            messages_by_room.setdefault(message["chatroom_id"], []).append(message)

        reads: dict[tuple[str, str], dict[str, str | None]] = {}
        for chatroom_id, user_id, last_read_message_id, last_read_at in read_rows:
            reads[(chatroom_id, user_id)] = {
                "last_read_message_id": last_read_message_id,
                "last_read_at": last_read_at,
            }

        reactions: dict[str, list[dict[str, str]]] = {}
        for message_id, user_id, emoji in reaction_rows:
            reactions.setdefault(message_id, []).append({"user_id": user_id, "emoji": emoji})

        return messages_by_room, reads, reactions

    def _upsert_message(self, message: ChatMessage) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO chat_messages (id, chatroom_id, sender_user_id, created, message_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    chatroom_id = excluded.chatroom_id,
                    sender_user_id = excluded.sender_user_id,
                    created = excluded.created,
                    message_json = excluded.message_json
                """,
                (
                    message["id"],
                    message["chatroom_id"],
                    message.get("sender_user_id"),
                    message["created"],
                    json.dumps(message),
                ),
            )
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception(
                "K93 ANS failed writing chat message %s to the database", message.get("id")
            )
        finally:
            conn.close()

    def _upsert_read(self, chatroom_id: str, user_id: str, message_id: str | None, at: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO chat_reads (chatroom_id, user_id, last_read_message_id, last_read_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chatroom_id, user_id) DO UPDATE SET
                    last_read_message_id = excluded.last_read_message_id,
                    last_read_at = excluded.last_read_at
                """,
                (chatroom_id, user_id, message_id, at),
            )
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception(
                "K93 ANS failed writing chat read state for chatroom %s / user %s",
                chatroom_id,
                user_id,
            )
        finally:
            conn.close()

    def _delete_message_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        conn = self._connect()
        try:
            conn.executemany("DELETE FROM chat_messages WHERE id = ?", [(i,) for i in ids])
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception("K93 ANS failed deleting chat messages from the database")
        finally:
            conn.close()

    def _add_reaction(self, message_id: str, user_id: str, emoji: str, created: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO chat_reactions (message_id, user_id, emoji, created) "
                "VALUES (?, ?, ?, ?)",
                (message_id, user_id, emoji, created),
            )
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception(
                "K93 ANS failed writing chat reaction %s on message %s", emoji, message_id
            )
        finally:
            conn.close()

    def _remove_reaction(self, message_id: str, user_id: str, emoji: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM chat_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
                (message_id, user_id, emoji),
            )
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception(
                "K93 ANS failed removing chat reaction %s on message %s", emoji, message_id
            )
        finally:
            conn.close()

    def _delete_reactions_for_messages(self, message_ids: list[str]) -> None:
        if not message_ids:
            return
        conn = self._connect()
        try:
            conn.executemany(
                "DELETE FROM chat_reactions WHERE message_id = ?", [(i,) for i in message_ids]
            )
            conn.commit()
        except sqlite3.Error:
            _LOGGER.exception("K93 ANS failed deleting chat reactions from the database")
        finally:
            conn.close()


    async def async_load(self) -> None:
        self._messages, self._reads, self._reactions = await self._hass.async_add_executor_job(
            self._load_all
        )

    async def async_add_message(self, message: ChatMessage) -> None:
        self._messages.setdefault(message["chatroom_id"], []).insert(0, message)
        await self._hass.async_add_executor_job(self._upsert_message, message)

    def async_list_messages(
        self, chatroom_id: str, limit: int | None = None, before_id: str | None = None
    ) -> list[ChatMessage]:
        messages = self._messages.get(chatroom_id, [])
        if before_id:
            cursor = next((m for m in messages if m["id"] == before_id), None)
            if cursor:
                messages = [m for m in messages if m["created"] < cursor["created"]]
        if limit is not None:
            messages = messages[:limit]
        return messages

    def async_latest_message(self, chatroom_id: str) -> ChatMessage | None:
        messages = self._messages.get(chatroom_id, [])
        return messages[0] if messages else None

    def async_message_belongs_to_room(self, chatroom_id: str, message_id: str) -> bool:
        return any(m["id"] == message_id for m in self._messages.get(chatroom_id, []))

    def async_get_reactions(self, message_id: str) -> list[dict[str, str]]:
        return list(self._reactions.get(message_id, []))

    async def async_add_reaction(self, message_id: str, user_id: str, emoji: str) -> None:
        existing = self._reactions.setdefault(message_id, [])
        if not any(r["user_id"] == user_id and r["emoji"] == emoji for r in existing):
            existing.append({"user_id": user_id, "emoji": emoji})
        await self._hass.async_add_executor_job(
            self._add_reaction, message_id, user_id, emoji, dt_util.utcnow().isoformat()
        )

    async def async_remove_reaction(self, message_id: str, user_id: str, emoji: str) -> None:
        existing = self._reactions.get(message_id)
        if existing:
            self._reactions[message_id] = [
                r for r in existing if not (r["user_id"] == user_id and r["emoji"] == emoji)
            ]
        await self._hass.async_add_executor_job(self._remove_reaction, message_id, user_id, emoji)

    def async_get_read_watermark(self, chatroom_id: str, user_id: str) -> dict[str, str | None] | None:
        return self._reads.get((chatroom_id, user_id))

    def async_room_read_states(self, chatroom_id: str) -> list[dict[str, str | None]]:
        return [
            {"user_id": user_id, "last_read_at": watermark["last_read_at"]}
            for (room_id, user_id), watermark in self._reads.items()
            if room_id == chatroom_id
        ]

    async def async_mark_read(
        self, chatroom_id: str, user_id: str, message_id: str | None, at: str
    ) -> None:
        self._reads[(chatroom_id, user_id)] = {"last_read_message_id": message_id, "last_read_at": at}
        await self._hass.async_add_executor_job(self._upsert_read, chatroom_id, user_id, message_id, at)

    def async_unread_count(self, chatroom_id: str, user_id: str) -> int:
        messages = self._messages.get(chatroom_id, [])
        watermark = self._reads.get((chatroom_id, user_id))
        if watermark is None:
            return len(messages)
        last_read_at = watermark["last_read_at"] or ""
        return sum(1 for m in messages if m["created"] > last_read_at)

    async def async_prune(
        self,
        chatroom_defs: list[dict[str, Any]],
        default_max_days: int,
        default_max_messages: int,
    ) -> None:
        room_lookup = {c["id"]: c for c in chatroom_defs}
        now = dt_util.utcnow()
        removed_ids: list[str] = []

        for chatroom_id, messages in self._messages.items():
            room = room_lookup.get(chatroom_id)
            max_days = (room.get("history_max_days") if room else None) or default_max_days
            max_messages = (room.get("history_max_messages") if room else None) or default_max_messages

            kept_by_age = [
                m
                for m in messages
                if dt_util.parse_datetime(m["created"]) >= now - timedelta(days=max_days)
            ]
            kept = kept_by_age[:max_messages]
            if len(kept) != len(messages):
                kept_ids = {m["id"] for m in kept}
                removed_ids.extend(m["id"] for m in messages if m["id"] not in kept_ids)
                self._messages[chatroom_id] = kept

        if removed_ids:
            await self._hass.async_add_executor_job(self._delete_message_ids, removed_ids)
            for message_id in removed_ids:
                self._reactions.pop(message_id, None)
            await self._hass.async_add_executor_job(self._delete_reactions_for_messages, removed_ids)

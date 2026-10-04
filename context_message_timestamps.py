"""Persistent, out-of-band timestamps for AstrBot conversation messages."""

from __future__ import annotations

import sqlite3
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .context_editor_core import message_hash


class ConversationMessageTimestampStore:
    """Keep UI timestamps outside provider-facing LLM message dictionaries."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (
            Path(get_astrbot_plugin_data_path())
            / "astrbot_plugin_private_companion"
            / "context_message_timestamps.db"
        )
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS message_timestamps (
                conversation_id TEXT NOT NULL,
                source_index INTEGER NOT NULL,
                message_hash TEXT NOT NULL,
                recorded_at REAL NOT NULL,
                PRIMARY KEY (conversation_id, source_index)
            )
            """
        )
        return connection

    def timestamps_for(
        self,
        conversation_id: str,
        history: list[Any],
    ) -> dict[int, float]:
        """Return timestamps only when both index and message hash still match."""

        if not conversation_id or not history:
            return {}
        hashes = [message_hash(message) for message in history]
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_index, message_hash, recorded_at
                FROM message_timestamps
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchall()
        return {
            int(index): float(recorded_at)
            for index, stored_hash, recorded_at in rows
            if 0 <= int(index) < len(hashes) and stored_hash == hashes[int(index)]
        }

    def record_update(
        self,
        conversation_id: str,
        old_history: list[Any],
        new_history: list[Any],
        *,
        recorded_at: float | None = None,
    ) -> None:
        """Preserve known values and stamp only an append-only message suffix."""

        if not conversation_id:
            return
        old_hashes = [message_hash(message) for message in old_history]
        new_hashes = [message_hash(message) for message in new_history]
        now = float(recorded_at if recorded_at is not None else time.time())

        with self._lock, self._connect() as connection:
            existing = {
                int(index): (stored_hash, float(timestamp))
                for index, stored_hash, timestamp in connection.execute(
                    """
                    SELECT source_index, message_hash, recorded_at
                    FROM message_timestamps
                    WHERE conversation_id = ?
                    """,
                    (conversation_id,),
                ).fetchall()
            }

            retained: dict[int, tuple[str, float]] = {}
            matcher = SequenceMatcher(None, old_hashes, new_hashes, autojunk=False)
            for old_start, new_start, size in matcher.get_matching_blocks():
                for offset in range(size):
                    previous = existing.get(old_start + offset)
                    if previous and previous[0] == new_hashes[new_start + offset]:
                        retained[new_start + offset] = previous

            common_prefix = 0
            prefix_limit = min(len(old_hashes), len(new_hashes))
            while (
                common_prefix < prefix_limit
                and old_hashes[common_prefix] == new_hashes[common_prefix]
            ):
                common_prefix += 1
            if common_prefix == len(old_hashes):
                for index in range(len(old_hashes), len(new_hashes)):
                    retained[index] = (new_hashes[index], now)

            connection.execute(
                "DELETE FROM message_timestamps WHERE conversation_id = ?",
                (conversation_id,),
            )
            connection.executemany(
                """
                INSERT INTO message_timestamps
                    (conversation_id, source_index, message_hash, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (conversation_id, index, stored_hash, timestamp)
                    for index, (stored_hash, timestamp) in sorted(retained.items())
                ],
            )


_STORE: ConversationMessageTimestampStore | None = None


def get_message_timestamp_store() -> ConversationMessageTimestampStore:
    global _STORE
    if _STORE is None:
        _STORE = ConversationMessageTimestampStore()
    return _STORE


__all__ = [
    "ConversationMessageTimestampStore",
    "get_message_timestamp_store",
]

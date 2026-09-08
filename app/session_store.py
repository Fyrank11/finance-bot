"""Durable aiogram drafts stored alongside the budget database.

This preserves conversations across process restarts, not Telegram update delivery.
Session payloads are JSON values; no executable Python serialization is used.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
import json
import logging
import math
from pathlib import Path
import time
from typing import Any

import aiosqlite
from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey


logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 60
CLEANUP_BATCH_SIZE = 100
SCHEMA = """
CREATE TABLE IF NOT EXISTS fsm_sessions (
    session_key TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    state TEXT,
    data TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fsm_sessions_expiry ON fsm_sessions(expires_at);
"""


def _key(key: StorageKey) -> str:
    # A JSON tuple distinguishes None, 0, empty strings and embedded separators.
    return json.dumps([
        key.bot_id, key.chat_id, key.user_id, key.thread_id,
        key.business_connection_id, key.destiny,
    ], ensure_ascii=False, separators=(",", ":"))


def _encode(data: Mapping[str, Any]) -> str:
    if not isinstance(data, Mapping):
        raise DataNotDictLikeError(
            f"Data must be a dict or dict-like object, got {type(data).__name__}"
        )
    if any(not isinstance(key, str) for key in data):
        raise TypeError("Session data keys must be strings")
    return json.dumps(dict(data), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _invalid_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


class SQLiteStorage(BaseStorage):
    """Atomic per-key state/data storage with a 30-day draft lifetime.

    Each operation owns a short SQLite transaction, including read/modify/write
    updates, so independent storage instances cannot lose simultaneous updates.
    Only writes extend the draft lifetime. Lazy cleanup removes at most 100 other
    expired rows per minute; an addressed expired row is always discarded.
    """

    def __init__(self, path: str | Path, *, ttl_seconds: float = DEFAULT_TTL_SECONDS):
        if not 0 < ttl_seconds < float("inf"):
            raise ValueError("Session TTL must be finite and positive")
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._cleanup_after = 0.0

    async def init(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.path, timeout=30) as connection:
                # Database.init owns journal mode. Reconfiguring it from two
                # independent storage instances can fail before busy_timeout.
                await connection.executescript(SCHEMA)
                await connection.commit()
            self._initialized = True

    @asynccontextmanager
    async def _transaction(self, key: StorageKey):
        await self.init()
        encoded_key = _key(key)
        async with aiosqlite.connect(self.path, timeout=30) as connection:
            # Acquire the write lock before reading, including across processes.
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                if now >= self._cleanup_after:
                    await connection.execute(
                        "DELETE FROM fsm_sessions WHERE session_key IN ("
                        "SELECT session_key FROM fsm_sessions WHERE expires_at<=? "
                        "ORDER BY expires_at LIMIT ?)",
                        (now, CLEANUP_BATCH_SIZE),
                    )
                    self._cleanup_after = now + CLEANUP_INTERVAL_SECONDS
                async with connection.execute(
                    "SELECT schema_version,state,data,expires_at FROM fsm_sessions "
                    "WHERE session_key=?", (encoded_key,),
                ) as cursor:
                    row = await cursor.fetchone()
                state, data = None, {}
                if row is not None:
                    corrupt = False
                    try:
                        if row[0] != SCHEMA_VERSION or not (row[1] is None or isinstance(row[1], str)):
                            raise ValueError("Unsupported session record")
                        decoded = json.loads(
                            row[2], parse_constant=_invalid_json_constant, parse_float=_json_float,
                        )
                        if not isinstance(decoded, dict):
                            raise ValueError("Session payload must be an object")
                        if not isinstance(row[3], (float, int)) or not math.isfinite(row[3]):
                            raise ValueError("Invalid expiry")
                        if row[3] > now:
                            state, data = row[1], decoded
                    except (TypeError, ValueError):
                        corrupt = True
                    if corrupt or row[3] <= now:
                        await connection.execute(
                            "DELETE FROM fsm_sessions WHERE session_key=?", (encoded_key,),
                        )
                        if corrupt:
                            # Never log user identifiers, draft contents or parser exceptions.
                            logger.warning("Discarded an invalid persisted conversation session")
                yield connection, encoded_key, now, state, data
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _write(self, connection, encoded_key, now, state, data_json):
        if state is None and data_json == "{}":
            await connection.execute("DELETE FROM fsm_sessions WHERE session_key=?", (encoded_key,))
            return
        await connection.execute(
            "INSERT INTO fsm_sessions(session_key,schema_version,state,data,expires_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(session_key) DO UPDATE SET "
            "schema_version=excluded.schema_version,state=excluded.state,"
            "data=excluded.data,expires_at=excluded.expires_at",
            (encoded_key, SCHEMA_VERSION, state, data_json, now + self.ttl_seconds),
        )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        if value is not None and not isinstance(value, str):
            raise TypeError("Session state must be a string, State or None")
        async with self._transaction(key) as (connection, encoded_key, now, _, data):
            await self._write(connection, encoded_key, now, value, _encode(data))

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._transaction(key) as (_connection, _encoded_key, _now, state, _data):
            return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        data_json = _encode(data)
        async with self._transaction(key) as (connection, encoded_key, now, state, _):
            await self._write(connection, encoded_key, now, state, data_json)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._transaction(key) as (_connection, _encoded_key, _now, _state, data):
            return data

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        # Snapshot before awaiting so callers cannot mutate queued updates.
        additions = json.loads(_encode(data))
        async with self._transaction(key) as (connection, encoded_key, now, state, current):
            current.update(additions)
            await self._write(connection, encoded_key, now, state, _encode(current))
            return current

    async def close(self) -> None:
        # Connections close after every operation; drafts remain on disk.
        pass

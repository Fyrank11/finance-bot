"""Persistent menu versions and graceful shutdown for one polling process."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import weakref
from typing import Any

import aiosqlite
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


APP_VERSION = "2026.09.08-savings.1"
MENU_UPDATED_TEXT = (
    "Меню обновлено. «Накопления и цели» — план взносов, резерв и полезные привычки. "
    "Перезапускать бот или повторять /start не нужно."
)
_UI_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_ui_seen (
    user_id INTEGER PRIMARY KEY CHECK(user_id > 0),
    version TEXT NOT NULL
)
"""
# The production access middleware serializes events. These weak locks also
# prevent duplicate notices from concurrent callers without retaining users.
_menu_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_logger = logging.getLogger(__name__)


def get_release_version() -> str:
    """Return a bounded public identifier, never arbitrary environment text."""
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    return commit[:12].lower() if re.fullmatch(r"[0-9a-fA-F]{40}", commit) else APP_VERSION


def _valid_user_id(user_id: int) -> None:
    if type(user_id) is not int or not 0 < user_id < 2**63:
        raise ValueError("Некорректный Telegram ID")


async def init_runtime(db) -> None:
    """Initialize after Database.init(); never change any budget table."""
    async with aiosqlite.connect(db.path) as connection:
        await connection.execute(_UI_SCHEMA)
        await connection.commit()


async def mark_ui_seen(db, user_id: int, *, version: str | None = None) -> None:
    """Call only after Telegram successfully receives the current main menu."""
    _valid_user_id(user_id)
    async with aiosqlite.connect(db.path) as connection:
        await connection.execute(_UI_SCHEMA)
        await connection.execute(
            "INSERT INTO bot_ui_seen(user_id,version) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET version=excluded.version",
            (user_id, version if version is not None else get_release_version()),
        )
        await connection.commit()


async def refresh_menu_if_needed(db, event, state, main_menu) -> bool:
    """Refresh an authenticated user's keyboard without interrupting a form.

    Call inside access authorization, preferably after the handler. /start and
    /menu render the keyboard themselves; their successful handlers must call
    mark_ui_seen. No startup broadcasts or silent marking before delivery.
    """
    if isinstance(event, CallbackQuery):
        message = event.message
    elif isinstance(event, Message):
        message = event
        command = (message.text or "").strip().split(maxsplit=1)
        if command and command[0].split("@", 1)[0].lower() in {"/start", "/menu"}:
            return False
    else:
        return False
    user = event.from_user
    if (not isinstance(message, Message) or message.chat.type != "private"
            or user is None or user.is_bot or type(user.id) is not int
            or not 0 < user.id < 2**63 or state is None):
        return False

    key = (asyncio.get_running_loop(), str(db.path.resolve()), user.id)
    lock = _menu_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if await state.get_state() is not None:
            return False
        version = get_release_version()
        async with aiosqlite.connect(db.path) as connection:
            await connection.execute(_UI_SCHEMA)
            row = await (await connection.execute(
                "SELECT version FROM bot_ui_seen WHERE user_id=?", (user.id,),
            )).fetchone()
            await connection.commit()
        if row is not None and row[0] == version:
            return False
        await message.answer(MENU_UPDATED_TEXT, reply_markup=main_menu)
        await mark_ui_seen(db, user.id, version=version)
        return True


class InFlightUpdates(BaseMiddleware):
    """Track event tasks before authorization/serialization, then drain them.

    Install as the outermost update middleware where possible. Register
    shutdown on the dispatcher; it runs after polling stops and before closing
    persistent FSM storage and the Bot session. One instance serves all events.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    async def __call__(self, handler, event, data):
        task = asyncio.current_task()
        # Nested use of the same tracker must not remove an outer registration.
        added = task is not None and task not in self._tasks
        if added:
            self._tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            if added:
                self._tasks.discard(task)

    async def drain(self, timeout: float = 20) -> None:
        if timeout < 0:
            raise ValueError("Shutdown timeout must not be negative")
        current = asyncio.current_task()
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            pending = {task for task in self._tasks if task is not current and not task.done()}
            if not pending:
                return
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            _, unfinished = await asyncio.wait(pending, timeout=remaining)
            if unfinished:
                _logger.warning("Shutdown deadline reached; cancelling %d pending updates", len(unfinished))
                for task in unfinished:
                    task.cancel()
                await asyncio.gather(*unfinished, return_exceptions=True)
                return

    async def shutdown(self, **kwargs) -> None:
        await self.drain(timeout=20)

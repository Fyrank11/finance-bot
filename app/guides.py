"""Optional visual instructions, with durable first-use delivery per person.

These cards contain product help only. They never read a budget or change a
financial form. Callers must authorize the Telegram user before invoking them.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import sqlite3
import weakref

import aiosqlite
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message

from .guide_catalog import GUIDE_CATALOG
from .guide_art import render_guide
from .keyboards import inline


GUIDE_VERSION = "1"
WELCOME_POSTER = Path(__file__).parent / "assets" / "welcome-poster.png"
WELCOME_CAPTION = (
    "Ваш бюджет — понятнее, планы — конкретнее.\n\n"
    "1. Запишите доход или расход: кнопками либо сообщением «продукты 850».\n"
    "2. Посмотрите сводку и ближайшие платежи.\n"
    "3. Добавьте цель и выберите посильный взнос.\n\n"
    "Бот считает по вашим записям: банковские счета пока не подключены. "
    "В каждом разделе есть графическая инструкция; открыть ее снова можно через справку."
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS guide_seen (
    user_id INTEGER NOT NULL CHECK(user_id > 0),
    topic TEXT NOT NULL,
    version TEXT NOT NULL,
    PRIMARY KEY(user_id, topic)
)
"""
_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_logger = logging.getLogger(__name__)
_DELIVERY_ERRORS = (TelegramAPIError, OSError, TimeoutError)
DELIVERY_TIMEOUT_SECONDS = 10


async def init_guides(db) -> None:
    """Add only the help-delivery table; existing budget data is untouched."""
    async with aiosqlite.connect(db.path) as connection:
        await connection.execute(_SCHEMA)
        await connection.commit()


def _valid_recipient(message, user_id) -> bool:
    # A callback's message was authored by the bot, so use its private chat,
    # not message.from_user. Authorization is the calling middleware's job.
    return (
        type(user_id) is int and 0 < user_id < 2**63
        and isinstance(message, Message)
        and message.chat.type == "private" and message.chat.id == user_id
    )


def _valid_topic(topic) -> bool:
    return isinstance(topic, str) and bool(re.fullmatch(r"[a-z][a-z0-9_]{0,39}", topic)) and topic in GUIDE_CATALOG


def guide_caption(topic: str) -> str:
    """The same instructions as the card, available as selectable plain text."""
    if not _valid_topic(topic):
        raise ValueError("Неизвестная инструкция")
    card = GUIDE_CATALOG[topic]
    parts = [card["title"], f"Зачем: {card['purpose']}", f"Что указать: {card['inputs']}", f"Что получите: {card['result']}"]
    if card.get("example"):
        example = card["example"]
        parts.append(example if example.lower().startswith("пример:") else f"Пример: {example}")
    parts.append(f"Учтите: {card['note']}")
    caption = "\n\n".join(parts)
    if len(caption) > 1024:
        raise ValueError("Инструкция превышает размер подписи Telegram")
    return caption


async def _seen(db, user_id: int, topic: str) -> bool:
    async with aiosqlite.connect(db.path) as connection:
        await connection.execute(_SCHEMA)
        row = await (await connection.execute(
            "SELECT version FROM guide_seen WHERE user_id=? AND topic=?", (user_id, topic),
        )).fetchone()
        await connection.commit()
    return row is not None and row[0] == GUIDE_VERSION


async def _mark_seen(db, user_id: int, topic: str) -> None:
    async with aiosqlite.connect(db.path) as connection:
        await connection.execute(_SCHEMA)
        await connection.execute(
            "INSERT INTO guide_seen(user_id,topic,version) VALUES(?,?,?) "
            "ON CONFLICT(user_id,topic) DO UPDATE SET version=excluded.version",
            (user_id, topic, GUIDE_VERSION),
        )
        await connection.commit()


async def _fallback(message: Message, text: str, markup) -> None:
    try:
        await asyncio.wait_for(message.answer(text, reply_markup=markup, parse_mode=None), timeout=DELIVERY_TIMEOUT_SECONDS)
    except _DELIVERY_ERRORS:
        # Exceptions can contain request URLs; do not log their details.
        _logger.warning("Could not deliver optional guide text")


async def _deliver(db, message, user_id: int, topic: str, *, force: bool, welcome: bool = False) -> bool:
    if not _valid_recipient(message, user_id) or (not welcome and not _valid_topic(topic)):
        return False
    key = (asyncio.get_running_loop(), str(Path(db.path).resolve()), user_id, topic)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        try:
            if not force and await _seen(db, user_id, topic):
                return False
        except (sqlite3.Error, OSError):
            _logger.warning("Could not read optional guide delivery status")
            return False

        if welcome:
            caption = WELCOME_CAPTION
            markup = inline([[("Начать учет", "nav:begin")], [("Познакомиться с ботом", "nav:help")]])
        else:
            try:
                caption = guide_caption(topic)
            except (ValueError, KeyError):
                _logger.warning("Invalid optional guide caption")
                return False
            markup = inline([[("ℹ️ Как это работает", f"guide:{topic}")]])

        try:
            if welcome:
                if not WELCOME_POSTER.is_file():
                    raise FileNotFoundError
                photo = FSInputFile(WELCOME_POSTER)
            else:
                rendered = await asyncio.to_thread(render_guide, topic)
                photo = BufferedInputFile(rendered, filename=f"guide-{topic}.png")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            _logger.warning("Could not render optional guide image")
            await _fallback(message, caption, markup)
            return False

        try:
            await asyncio.wait_for(message.answer_photo(photo, caption=caption, reply_markup=markup, parse_mode=None), timeout=DELIVERY_TIMEOUT_SECONDS)
        except _DELIVERY_ERRORS:
            _logger.warning("Could not deliver optional guide image")
            await _fallback(message, caption, markup)
            return False

        try:
            await _mark_seen(db, user_id, topic)
        except (sqlite3.Error, OSError):
            _logger.warning("Could not persist optional guide delivery status")
        return True


async def maybe_show(db, message, user_id: int, topic: str, *, force: bool = False) -> bool:
    """Send once per guide version, or on request; return actual image delivery."""
    return await _deliver(db, message, user_id, topic, force=force)


async def show_welcome(db, message, user_id: int, *, force: bool = False) -> bool:
    """The static welcome poster has its own durable per-person seen flag."""
    return await _deliver(db, message, user_id, "welcome", force=force, welcome=True)


async def handle_callback(callback: CallbackQuery, state, db) -> bool:
    """Replay generic help without changing the current input or budget scope."""
    data = callback.data or ""
    if not data.startswith("guide:"):
        return False
    topic = data[len("guide:"):]
    user = callback.from_user
    valid = (not user.is_bot and _valid_recipient(callback.message, user.id)
             and (topic == "welcome" or _valid_topic(topic)))
    try:
        await callback.answer(None if valid else "Откройте инструкцию из справки: /help")
    except _DELIVERY_ERRORS:
        _logger.warning("Could not acknowledge optional guide callback")
    if valid:
        if topic == "welcome":
            await show_welcome(db, callback.message, user.id, force=True)
        else:
            await maybe_show(db, callback.message, user.id, topic, force=True)
    return True


_MESSAGE_TOPICS = {
    "➕ Доход": "income", "➖ Расход": "expense",
    "📊 Мой бюджет": "budget", "📊 Сводка": "budget", "📝 История": "history", "🎯 Лимиты": "limits",
    "📁 Скачать Excel": "export", "🗓 Платежи": "payments", "📈 Аналитика": "analytics",
    "🌱 Накопления": "savings", "🌱 Накопления и цели": "savings", "🧭 Распределение": "savings",
    "🎯 Цель": "goals", "🎯 Мои цели": "goals", "💡 Подсказки": "tips",
    "🔮 Прогноз": "forecast", "📬 Обзор недели": "weekly", "🔎 Поиск": "search",
    "👥 Семья": "family", "⚙️ Настройки": "settings", "🤝 Долг": "debts",
    "💬 Могу позволить?": "afford", "💳 Кредитка?": "credit",
}
_COMMAND_TOPICS = {
    "/payments": "payments", "/analytics": "analytics", "/charts": "analytics",
    "/savings": "savings", "/tips": "tips", "/forecast": "forecast",
    "/weekly": "weekly", "/search": "search", "/family": "family",
}


def topic_for_message(text: str | None) -> str | None:
    """Only explicit section entries, never amounts, free text, or form steps."""
    text = (text or "").strip()
    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    topic = _COMMAND_TOPICS.get(command) or _MESSAGE_TOPICS.get(text)
    return topic if _valid_topic(topic) else None


def topic_for_callback(data: str | None) -> str | None:
    """Map already-authorized, unwrapped view entries; never unwrap scope here."""
    data = data or ""
    exact = {
        "sav:home": "savings", "sav:reserve": "reserve", "sav:budget": "savings_budget",
        "cf:home": "forecast", "wk:home": "weekly",
        "analytics": "analytics", "opening": "opening", "pick_month": "month",
        "rec:home": "payments",
    }
    topic = exact.get(data)
    if topic is None and re.fullmatch(r"sav:goals:\d{1,2}", data):
        topic = "goals"
    if topic is None and re.fullmatch(r"(?:charts|tips):\d{4}-\d{2}", data):
        topic = "analytics" if data.startswith("charts:") else "tips"
    if topic is None and re.fullmatch(r"history:\d{4}-\d{2}:\d{1,3}", data):
        topic = "history"
    return topic if _valid_topic(topic) else None

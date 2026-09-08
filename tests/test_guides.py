import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from app import guides


def message(user_id=101, *, chat_id=None, chat_type="private", bot_author=False):
    return Message(
        message_id=1, date=datetime.now(timezone.utc), text="Инструкция",
        chat=Chat(id=user_id if chat_id is None else chat_id, type=chat_type),
        from_user=User(id=999 if bot_author else user_id, is_bot=bot_author, first_name="Test"),
    )


def callback(data, user_id=101, *, chat_id=None, chat_type="private"):
    return CallbackQuery(
        id="help", from_user=message(user_id).from_user, chat_instance="private",
        message=message(user_id, chat_id=chat_id, chat_type=chat_type, bot_author=True), data=data,
    )


def rows(db):
    with sqlite3.connect(db.path) as connection:
        return connection.execute("SELECT user_id,topic,version FROM guide_seen ORDER BY user_id,topic").fetchall()


@pytest.fixture
def db(tmp_path):
    return SimpleNamespace(path=tmp_path / "budget.db")


@pytest.fixture
def delivery(monkeypatch):
    sent = {"photos": [], "texts": [], "answers": []}

    async def photo(self, value, **kwargs):
        await asyncio.sleep(0)
        sent["photos"].append((self.chat.id, value, kwargs))

    async def answer(self, value, **kwargs):
        sent["texts"].append((self.chat.id, value, kwargs))

    async def acknowledge(self, *args, **kwargs):
        sent["answers"].append((args, kwargs))

    monkeypatch.setattr(Message, "answer_photo", photo)
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(CallbackQuery, "answer", acknowledge)
    monkeypatch.setattr(guides, "render_guide", lambda topic: b"test image")
    return sent


def test_first_use_survives_restart_and_is_separate_for_people_and_topics(db, delivery, monkeypatch):
    async def scenario():
        await guides.init_guides(db)
        assert await guides.maybe_show(db, message(), 101, "income")
        assert not await guides.maybe_show(db, message(), 101, "income")
        restarted = SimpleNamespace(path=Path(str(db.path)))
        await guides.init_guides(restarted)
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
        assert not await guides.maybe_show(restarted, message(), 101, "income")
        assert await guides.maybe_show(restarted, message(), 101, "expense")
        assert await guides.maybe_show(restarted, message(202), 202, "income")
        assert rows(db) == [(101, "expense", guides.GUIDE_VERSION), (101, "income", guides.GUIDE_VERSION), (202, "income", guides.GUIDE_VERSION)]
        assert len(delivery["photos"]) == 3

    asyncio.run(scenario())


def test_forced_replay_and_content_version(db, delivery, monkeypatch):
    async def scenario():
        assert await guides.maybe_show(db, message(), 101, "goals")
        assert await guides.maybe_show(db, message(), 101, "goals", force=True)
        monkeypatch.setattr(guides, "GUIDE_VERSION", "2")
        assert await guides.maybe_show(db, message(), 101, "goals")
        assert not await guides.maybe_show(db, message(), 101, "goals")
        assert rows(db) == [(101, "goals", "2")]
        assert len(delivery["photos"]) == 3

    asyncio.run(scenario())


def test_concurrent_first_use_delivers_one_card(db, delivery):
    async def scenario():
        results = await asyncio.gather(*(guides.maybe_show(db, message(), 101, "forecast") for _ in range(8)))
        assert results.count(True) == 1
        assert len(delivery["photos"]) == 1
        assert rows(db) == [(101, "forecast", guides.GUIDE_VERSION)]

    asyncio.run(scenario())


def test_all_captions_include_the_complete_public_instructions():
    for topic, card in guides.GUIDE_CATALOG.items():
        caption = guides.guide_caption(topic)
        assert len(caption) <= 1024
        for key in ("title", "purpose", "inputs", "result", "note"):
            assert card[key] in caption
        if card.get("example"):
            assert card["example"] in caption
    for invalid in ("../../tokens", "welcome", "income:extra", "INCOME", None):
        with pytest.raises(ValueError):
            guides.guide_caption(invalid)


def test_photo_carries_accessible_caption_and_repeat_help_button(db, delivery):
    async def scenario():
        assert await guides.maybe_show(db, message(), 101, "income")
        recipient, photo, arguments = delivery["photos"][0]
        assert recipient == 101
        assert photo.data == b"test image"
        assert photo.filename == "guide-income.png"
        assert arguments["caption"] == guides.guide_caption("income")
        assert arguments["parse_mode"] is None
        assert arguments["reply_markup"].inline_keyboard[0][0].callback_data == "guide:income"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [OSError("private path"), ValueError("render details"), RuntimeError("font details")])
def test_failed_render_falls_back_to_text_and_retries_later(db, delivery, monkeypatch, failure, caplog):
    def broken(topic):
        raise failure

    async def scenario():
        monkeypatch.setattr(guides, "render_guide", broken)
        assert not await guides.maybe_show(db, message(), 101, "reserve")
        assert rows(db) == []
        assert not delivery["photos"]
        assert delivery["texts"][0][1] == guides.guide_caption("reserve")
        monkeypatch.setattr(guides, "render_guide", lambda topic: b"retry")
        assert await guides.maybe_show(db, message(), 101, "reserve")
        assert len(rows(db)) == 1

    asyncio.run(scenario())
    assert str(failure) not in caplog.text


def test_failed_photo_and_failed_fallback_do_not_mark_or_interrupt(db, delivery, monkeypatch, caplog):
    async def broken(*args, **kwargs):
        raise ConnectionError("sensitive transport details")

    async def scenario():
        monkeypatch.setattr(Message, "answer_photo", broken)
        monkeypatch.setattr(Message, "answer", broken)
        assert not await guides.maybe_show(db, message(), 101, "weekly")
        assert rows(db) == []

    asyncio.run(scenario())
    assert "sensitive transport details" not in caplog.text


def test_photo_failure_successful_text_fallback_is_not_marked_seen(db, delivery, monkeypatch):
    async def broken(*args, **kwargs):
        raise TimeoutError("Telegram timeout")

    async def scenario():
        monkeypatch.setattr(Message, "answer_photo", broken)
        assert not await guides.maybe_show(db, message(), 101, "history")
        assert rows(db) == []
        assert len(delivery["texts"]) == 1

    asyncio.run(scenario())


def test_welcome_is_independent_once_per_person_with_navigation(db, delivery, monkeypatch, tmp_path):
    poster = tmp_path / "welcome.png"
    poster.write_bytes(b"poster")
    monkeypatch.setattr(guides, "WELCOME_POSTER", poster)

    async def scenario():
        assert await guides.show_welcome(db, message(), 101)
        assert not await guides.show_welcome(db, message(), 101)
        assert await guides.maybe_show(db, message(), 101, "income")
        assert await guides.show_welcome(db, message(202), 202)
        assert await guides.show_welcome(db, message(), 101, force=True)
        photo = delivery["photos"][0]
        assert Path(photo[1].path) == poster
        buttons = photo[2]["reply_markup"].inline_keyboard
        assert [row[0].callback_data for row in buttons] == ["nav:begin", "nav:help"]
        assert len(rows(db)) == 3

    asyncio.run(scenario())


def test_missing_welcome_image_falls_back_without_seen_flag(db, delivery, monkeypatch, tmp_path):
    monkeypatch.setattr(guides, "WELCOME_POSTER", tmp_path / "missing.png")

    async def scenario():
        assert not await guides.show_welcome(db, message(), 101)
        assert delivery["texts"][0][1] == guides.WELCOME_CAPTION
        assert rows(db) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("user_id", [True, 0, -1, "101", 2**63])
def test_invalid_user_id_never_opens_database(db, delivery, user_id):
    async def scenario():
        assert not await guides.maybe_show(db, message(), user_id, "income")
        assert not await guides.show_welcome(db, message(), user_id)
        assert not db.path.exists()
        assert not delivery["photos"] and not delivery["texts"]

    asyncio.run(scenario())


@pytest.mark.parametrize("topic", ["../income", "income.png", "income:extra", "welcome", "", None])
def test_invalid_topic_never_renders_or_reads_database(db, delivery, topic):
    async def scenario():
        assert not await guides.maybe_show(db, message(), 101, topic, force=True)
        assert not db.path.exists()
        assert not delivery["photos"] and not delivery["texts"]

    asyncio.run(scenario())


def test_private_recipient_must_match_user(db, delivery):
    async def scenario():
        for recipient in (message(chat_id=202), message(chat_id=-100, chat_type="group"), object()):
            assert not await guides.maybe_show(db, recipient, 101, "income")
            assert not await guides.show_welcome(db, recipient, 101)
        assert not db.path.exists()

    asyncio.run(scenario())


def test_callback_replay_preserves_form_and_accepts_bot_authored_message(db, delivery):
    class UntouchedState:
        def __getattr__(self, name):
            raise AssertionError("Help must not access or mutate form state")

    async def scenario():
        state = UntouchedState()
        assert await guides.handle_callback(callback("guide:income"), state, db)
        assert await guides.handle_callback(callback("guide:income"), state, db)
        assert len(delivery["photos"]) == 2
        assert len(rows(db)) == 1
        assert len(delivery["answers"]) == 2
        assert not await guides.handle_callback(callback("sav:home"), state, db)

    asyncio.run(scenario())


def test_invalid_or_foreign_callback_is_acknowledged_without_delivering(db, delivery):
    async def scenario():
        for event in (
            callback("guide:income:extra"), callback("guide:../../secrets"), callback("guide:"),
            callback("guide:income", chat_id=202), callback("guide:income", chat_type="group", chat_id=-100),
            CallbackQuery(id="inline", from_user=message().from_user, chat_instance="x", inline_message_id="x", data="guide:income"),
        ):
            assert await guides.handle_callback(event, object(), db)
        assert len(delivery["answers"]) == 6
        assert not db.path.exists()
        assert not delivery["photos"]

    asyncio.run(scenario())


def test_optional_database_failure_does_not_block_the_caller(db, delivery, monkeypatch):
    async def scenario():
        missing = SimpleNamespace(path=db.path.parent / "missing" / "budget.db")
        assert not await guides.maybe_show(missing, message(), 101, "income")

        async def broken_mark(*args):
            raise sqlite3.OperationalError("do not log private database contents")

        monkeypatch.setattr(guides, "_mark_seen", broken_mark)
        assert await guides.maybe_show(db, message(), 101, "income")
        assert rows(db) == []

    asyncio.run(scenario())


def test_initialization_is_additive_and_has_no_budget_queries(db):
    with sqlite3.connect(db.path) as connection:
        connection.execute("CREATE TABLE private_records (id INTEGER, note TEXT)")
        connection.execute("INSERT INTO private_records VALUES (1, 'unchanged')")

    async def scenario():
        await guides.init_guides(db)
        await guides.init_guides(db)

    asyncio.run(scenario())
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT * FROM private_records").fetchall() == [(1, "unchanged")]
        assert {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"private_records", "guide_seen"}


@pytest.mark.parametrize("text,expected", [
    ("➕ Доход", "income"), ("📊 Сводка", "budget"), ("🧭 Распределение", "savings"),
    ("🎯 Цель", "goals"), ("🤝 Долг", "debts"), ("/charts", "analytics"),
    ("/forecast@finance_bot", "forecast"), ("/search кофе", "search"),
    ("/weekly", "weekly"), ("850", None), ("продукты 850", None), ("/start", None),
    ("/cancel", None), ("/help", None), (None, None),
])
def test_message_mapping_only_explicit_feature_entries(text, expected):
    assert guides.topic_for_message(text) == expected


@pytest.mark.parametrize("data,expected", [
    ("sav:home", "savings"), ("sav:goals:0", "goals"), ("sav:reserve", "reserve"),
    ("sav:budget", "savings_budget"), ("cf:home", "forecast"), ("wk:home", "weekly"),
    ("opening", "opening"), ("pick_month", "month"), ("charts:2026-09", "analytics"),
    ("tips:2026-09", "tips"), ("history:2026-09:0", "history"),
    ("scope:101:1:sav:home", None), ("sav:confirm:12345678", None), ("cf:close:1", None),
    ("wk:disable:12345678", None), ("delete:123", None), ("sav:goals:0:extra", None),
    ("guide:income", None), (None, None),
])
def test_callback_mapping_never_unwraps_scope_or_maps_mutations(data, expected):
    assert guides.topic_for_callback(data) == expected

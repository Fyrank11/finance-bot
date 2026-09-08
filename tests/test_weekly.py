"""Real SQLite tests for private reports, calendar scheduling and durable claims."""
import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from app import family, recurring, weekly
from app.access import register_user
from app.db import Database

UTC = timezone.utc
BEFORE = datetime(2026, 9, 6, 12, tzinfo=UTC)
DUE = datetime(2026, 9, 7, 6, tzinfo=UTC)  # Monday 09:00 Moscow


async def database(tmp_path):
    db = Database(tmp_path / "weekly.db")
    await db.init()
    await weekly.init_weekly(db)
    return db


async def subscribe(db, user_id=1, **overrides):
    fields = dict(budget_id=user_id, weekday=0, hour=9, minute=0,
                  timezone="Europe/Moscow", enabled=True, expected_version=None, now=BEFORE)
    fields.update(overrides)
    return await weekly.set_subscription(db, user_id, **fields)


async def dispatch(db, bot, **overrides):
    fields = dict(access_lock=asyncio.Lock(), allowed_user_ids=frozenset({1, 2, 3}),
                  public_signup=False, now=DUE)
    fields.update(overrides)
    return await weekly.dispatch_due(bot, db, **fields)


def test_next_occurrence_is_strict_future_and_local_date():
    assert weekly.next_due(DUE, weekday=0, hour=9, minute=0, timezone="Europe/Moscow") == DUE + timedelta(days=7)
    assert weekly.next_due(DUE - timedelta(seconds=1), weekday=0, hour=9, minute=0,
                           timezone="Europe/Moscow") == DUE
    # UTC Sunday, but already Monday at 00:00 in Tokyo.
    now = datetime(2026, 9, 6, 15, tzinfo=UTC)
    assert weekly.next_due(now, weekday=0, hour=0, minute=30,
                           timezone="Asia/Tokyo") == now + timedelta(minutes=30)


def test_dst_gap_moves_forward_and_fold_runs_only_once():
    gap = weekly.next_due(datetime(2026, 3, 28, tzinfo=UTC), weekday=6, hour=2, minute=30,
                          timezone="Europe/Berlin")
    assert gap == datetime(2026, 3, 29, 1, tzinfo=UTC)  # first valid local time 03:00
    fold = weekly.next_due(datetime(2026, 10, 24, tzinfo=UTC), weekday=6, hour=2, minute=30,
                           timezone="Europe/Berlin")
    assert fold == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert weekly.next_due(fold + timedelta(minutes=15), weekday=6, hour=2, minute=30,
                           timezone="Europe/Berlin") == datetime(2026, 11, 1, 1, 30, tzinfo=UTC)


def test_subscription_default_off_and_optimistic_changes_survive_restart(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        assert await weekly.get_subscription(db, 1) is None
        assert await subscribe(db, enabled=False)
        row = await weekly.get_subscription(db, 1)
        assert row["enabled"] == 0 and row["next_due_at"] is None and row["version"] == 1
        assert not await subscribe(db)  # Creation cannot overwrite existing settings.
        changes = await asyncio.gather(subscribe(db, expected_version=1), subscribe(db, expected_version=1))
        assert sum(changes) == 1
        reloaded = Database(db.path)
        await reloaded.init()
        await weekly.init_weekly(reloaded)
        row = await weekly.get_subscription(reloaded, 1)
        assert row["version"] == 2 and row["enabled"] == 1
        assert datetime.fromisoformat(row["next_due_at"]) == DUE
        assert not await subscribe(db, expected_version=1, enabled=False)
        assert await subscribe(db, expected_version=2, enabled=False)
        assert await dispatch(db, AsyncMock()) == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [
    {"weekday": True}, {"weekday": 7}, {"hour": 24}, {"minute": -1},
    {"timezone": "Mars/Unknown"}, {"timezone": "/etc/passwd"}, {"enabled": 1},
    {"budget_id": 0}, {"budget_id": 2**63}, {"budget_id": 2},
    {"expected_version": True}, {"now": datetime(2026, 9, 7)},
])
def test_invalid_settings_create_nothing(tmp_path, overrides):
    async def scenario():
        db = await database(tmp_path)
        with pytest.raises(ValueError):
            await subscribe(db, **overrides)
        assert await weekly.get_subscription(db, 1) is None
    asyncio.run(scenario())


def test_family_is_explicit_and_switching_active_budget_does_not_retarget(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        household_budget = await family.create_household(db, 1, "Дом")
        assert household_budget < 0
        with pytest.raises(ValueError, match="доступа"):
            await subscribe(db, 2, budget_id=household_budget)
        assert await subscribe(db, budget_id=household_budget)
        await family.switch_budget(db, 1, False)
        await db.add_transaction(household_budget, "expense", "семейная трата", 800, occurred_on="2026-09-06")
        await db.add_transaction(1, "expense", "личная тайна", 900, occurred_on="2026-09-06")
        bot = AsyncMock()
        assert await dispatch(db, bot) == 1
        text = bot.send_message.call_args.kwargs["text"]
        assert "семейный" in text and "семейная трата" in text and "личная тайна" not in text
        assert (await weekly.get_subscription(db, 1))["budget_id"] == household_budget
    asyncio.run(scenario())


def test_due_once_across_concurrent_workers_and_restart(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await subscribe(db)
        bot = AsyncMock()
        assert await dispatch(db, bot, now=DUE - timedelta(seconds=1)) == 0
        sent = await asyncio.gather(dispatch(db, bot), dispatch(db, bot))
        assert sum(sent) == 1 and bot.send_message.await_count == 1
        reloaded = Database(db.path)
        await reloaded.init()
        await weekly.init_weekly(reloaded)
        assert await dispatch(reloaded, bot, now=DUE + timedelta(hours=1)) == 0
        assert await dispatch(reloaded, bot, now=DUE + timedelta(days=7)) == 1
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT status FROM weekly_deliveries ORDER BY scheduled_at").fetchall() == [("sent",), ("sent",)]
            assert conn.execute("SELECT count(*) FROM transactions").fetchone() == (0,)
    asyncio.run(scenario())


def test_backlog_sends_only_latest_within_24_hours_otherwise_skips(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await subscribe(db)
        bot = AsyncMock()
        # Three weeks offline: one eligible most-recent report, no backlog.
        assert await dispatch(db, bot, now=DUE + timedelta(days=21, hours=23)) == 1
        text = bot.send_message.call_args.kwargs["text"]
        assert "21.09.2026–27.09.2026" in text
        assert datetime.fromisoformat((await weekly.get_subscription(db, 1))["next_due_at"]) == DUE + timedelta(days=28)
        await subscribe(db, 2)
        assert await dispatch(db, bot, now=DUE + timedelta(days=21, hours=25)) == 0
        assert bot.send_message.await_count == 1
        assert datetime.fromisoformat((await weekly.get_subscription(db, 2))["next_due_at"]) == DUE + timedelta(days=28)
    asyncio.run(scenario())


def test_enabling_at_due_does_not_send_a_startup_report(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await subscribe(db, now=DUE)
        assert await dispatch(db, AsyncMock()) == 0
        assert datetime.fromisoformat((await weekly.get_subscription(db, 1))["next_due_at"]) == DUE + timedelta(days=7)
    asyncio.run(scenario())


def test_delivery_timezone_does_not_include_incomplete_bookkeeping_day(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        # Monday midnight in Kamchatka is still Sunday 15:00 in Moscow.
        due = datetime(2026, 9, 6, 12, tzinfo=UTC)
        await subscribe(db, timezone="Asia/Kamchatka", hour=0, now=due - timedelta(hours=1))
        await db.add_transaction(1, "expense", "воскресенье ещё идёт", 100, occurred_on="2026-09-06")
        await db.add_transaction(1, "expense", "завершённая суббота", 20, occurred_on="2026-09-05")
        bot = AsyncMock()
        assert await dispatch(db, bot, now=due) == 1
        text = bot.send_message.call_args.kwargs["text"]
        assert "30.08.2026–05.09.2026" in text and "Даты учёта: Europe/Moscow" in text
        assert "воскресенье ещё идёт" not in text and "Расходы: 20 ₽" in text
    asyncio.run(scenario())


def test_registration_and_current_allowlist_checked_before_each_send(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        for user in (1, 2, 3):
            await subscribe(db, user)
        await register_user(db, 2)
        bot = AsyncMock()
        assert await dispatch(db, bot, allowed_user_ids=frozenset({1}), public_signup=True) == 2
        assert {call.kwargs["chat_id"] for call in bot.send_message.call_args_list} == {1, 2}
        assert (await weekly.get_subscription(db, 3))["enabled"] == 0
        assert await dispatch(db, bot, allowed_user_ids=frozenset(), public_signup=False,
                              now=DUE + timedelta(days=7)) == 0
        assert (await weekly.get_subscription(db, 1))["enabled"] == 0
        assert (await weekly.get_subscription(db, 2))["enabled"] == 0
    asyncio.run(scenario())


def test_family_removal_while_report_queued_disables_without_sending(tmp_path):
    class ObservedLock:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.waiting = asyncio.Event()

        async def __aenter__(self):
            self.waiting.set()
            await self.lock.acquire()

        async def __aexit__(self, *args):
            self.lock.release()

    async def scenario():
        db = await database(tmp_path)
        budget = await family.create_household(db, 1, "Закрытый дом")
        code = await family.create_invite(db, 1)
        await family.join_household(db, 2, code)
        await subscribe(db, 2, budget_id=budget)
        lock, bot = ObservedLock(), AsyncMock()
        await lock.lock.acquire()
        task = asyncio.create_task(dispatch(db, bot, access_lock=lock))
        await asyncio.wait_for(lock.waiting.wait(), timeout=2)
        await family.remove_member(db, 1, 2)
        lock.lock.release()
        assert await task == 0
        bot.send_message.assert_not_awaited()
        row = await weekly.get_subscription(db, 2)
        assert row["enabled"] == 0
        # The removed member can still explicitly turn off their stale pinned settings.
        assert await subscribe(db, 2, budget_id=budget, enabled=False, expected_version=row["version"])
    asyncio.run(scenario())


def test_optout_after_candidate_read_prevents_send(tmp_path, monkeypatch):
    async def scenario():
        db = await database(tmp_path)
        await subscribe(db)
        real_claim = weekly._claim

        async def changed_before_claim(*args, **kwargs):
            assert await subscribe(db, enabled=False, expected_version=1)
            return await real_claim(*args, **kwargs)

        monkeypatch.setattr(weekly, "_claim", changed_before_claim)
        bot = AsyncMock()
        assert await dispatch(db, bot) == 0
        bot.send_message.assert_not_awaited()
    asyncio.run(scenario())


def test_crash_after_claim_is_not_retried_after_restart(tmp_path, monkeypatch):
    async def scenario():
        db = await database(tmp_path)
        await subscribe(db)
        original = weekly.build_digest
        monkeypatch.setattr(weekly, "build_digest", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await dispatch(db, AsyncMock())
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT status FROM weekly_deliveries").fetchone() == ("claimed",)
        monkeypatch.setattr(weekly, "build_digest", original)
        reloaded = Database(db.path)
        await weekly.init_weekly(reloaded)
        bot = AsyncMock()
        assert await dispatch(reloaded, bot) == 0
        assert await dispatch(reloaded, bot, now=DUE + timedelta(days=7)) == 1
    asyncio.run(scenario())


def test_send_failures_isolated_forbidden_disables_retryafter_not_retried(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        for user in (1, 2, 3):
            await subscribe(db, user)
        method = SendMessage(chat_id=1, text="example")
        bot = AsyncMock()
        bot.send_message.side_effect = [TelegramForbiddenError(method, "blocked"),
                                        TelegramRetryAfter(method, "rate limit", retry_after=30), None]
        assert await dispatch(db, bot) == 1
        assert (await weekly.get_subscription(db, 1))["enabled"] == 0
        assert (await weekly.get_subscription(db, 2))["enabled"] == 1
        assert await dispatch(db, bot, now=DUE + timedelta(minutes=5)) == 0
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT user_id,status FROM weekly_deliveries ORDER BY user_id").fetchall() == [(1, "blocked"), (2, "failed"), (3, "sent")]
    asyncio.run(scenario())


def test_worker_stop_event_interrupts_wait_without_broadcast(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        stop = asyncio.Event()
        bot = AsyncMock()
        task = asyncio.create_task(weekly.run_weekly_worker(bot, db, access_lock=asyncio.Lock(),
                                                          allowed_user_ids=frozenset({1}), public_signup=False,
                                                          stop_event=stop))
        await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        bot.send_message.assert_not_awaited()
    asyncio.run(scenario())


def test_stop_while_waiting_for_access_lock_does_not_claim_or_send(tmp_path):
    class StopAtLock:
        def __init__(self, stop):
            self.stop = stop

        async def __aenter__(self):
            self.stop.set()

        async def __aexit__(self, *args):
            pass

    async def scenario():
        db = await database(tmp_path)
        await subscribe(db)
        stop, bot = asyncio.Event(), AsyncMock()
        assert await dispatch(db, bot, access_lock=StopAtLock(stop), stop_event=stop) == 0
        bot.send_message.assert_not_awaited()
        assert datetime.fromisoformat((await weekly.get_subscription(db, 1))["next_due_at"]) == DUE
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT count(*) FROM weekly_deliveries").fetchone() == (0,)
    asyncio.run(scenario())


def test_ambiguous_send_timeout_is_not_retried(tmp_path, monkeypatch):
    async def scenario():
        db = await database(tmp_path)
        await subscribe(db)
        bot = AsyncMock()

        async def unfinished(**_kwargs):
            await asyncio.Event().wait()

        bot.send_message.side_effect = unfinished
        monkeypatch.setattr(weekly, "SEND_TIMEOUT", 0.01)
        assert await dispatch(db, bot) == 0
        assert await dispatch(db, bot, now=DUE + timedelta(hours=1)) == 0
        assert bot.send_message.await_count == 1
        assert (await weekly.get_subscription(db, 1))["enabled"] == 1
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT status FROM weekly_deliveries").fetchone() == ("failed",)
    asyncio.run(scenario())


def test_digest_completed_periods_money_isolation_upcoming_crosses_month_end(tmp_path, monkeypatch):
    async def scenario():
        db = await database(tmp_path)
        # Fixed as_of is historical; totals use occurrence dates, not insertion timestamps.
        as_of = date(2026, 3, 29)
        entries = [
            (1, "income", "зарплата", 1000, "2026-03-22"),
            (1, "expense", "кофе", 20, "2026-03-28"),
            (1, "expense", "еда", 30, "2026-03-23"),
            (1, "expense", "еда", 10, "2026-03-15"),
            (1, "income", "зарплата", 500, "2026-03-21"),
            (1, "expense", "сегодня не включать", 100, "2026-03-29"),
            (2, "expense", "чужая тайна", 999, "2026-03-28"),
        ]
        for user, kind, category, amount, day in entries:
            await db.add_transaction(user, kind, category, amount, occurred_on=day)
        await db.set_opening(1, 999999)
        await db.set_budget(1, "2026-03", "еда", 35)
        for name, day in (("Срок прошёл", 20), ("В апреле", 2), ("За пределами недели", 7), ("Уже оплачено", 30)):
            schedule = await recurring.create_schedule(db, 1, name=name, category="другое", amount_minor=1000,
                                                       day_of_month=day, start_month="2026-03" if day >= 20 else "2026-04")
            if day == 30:
                monkeypatch.setattr(recurring, "today", lambda _tz: as_of)
                await recurring.mark_paid(db, 1, schedule, "2026-03-30")
        with sqlite3.connect(db.path) as conn:
            before = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
        text = await weekly.build_digest(db, 1, as_of)
        assert "22.03.2026–28.03.2026" in text and "Доходы: 1 000 ₽" in text and "Расходы: 50 ₽" in text
        assert "950 ₽ (это не остаток денег)" in text and "больше на 40 ₽" in text
        assert "еда»" in text and "40 ₽ из 35 ₽" in text
        assert "В апреле" in text and "Срок прошёл" not in text and "срок прошёл у 1" in text
        assert "За пределами недели" not in text and "Уже оплачено" not in text
        assert "сегодня не включать" not in text and "чужая тайна" not in text and "999 999" not in text
        assert "Ожидаемые доходы не считаются полученными" in text and "/weekly" in text
        assert len(text) < 3500
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT * FROM transactions ORDER BY id").fetchall() == before
    asyncio.run(scenario())


def test_empty_digest_does_not_claim_economy_or_sufficient_cash(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        text = await weekly.build_digest(db, 1, date(2026, 9, 7))
        assert "Внесено операций: 0" in text
        assert "не означает экономию" in text and "Сверьте записи" in text
        assert "Расходы: 0 ₽" in text and "остаток: 0" not in text.lower()
    asyncio.run(scenario())

"""Verify the instructions actually lead to the selected transaction type."""
import asyncio

import pytest
from aiogram.methods import SendMessage, SendPhoto

from app import bot as app, guides
from app.inputs import today
from test_persistent_bot import PersistentChatHarness


def last_text(chat):
    return next(call.text for call in reversed(chat.session.calls) if isinstance(call, SendMessage))


@pytest.mark.parametrize("kind,button,label,category,example_amount", [
    ("income", "➕ Доход", "дохода", "Зарплата", 80000),
    ("expense", "➖ Расход", "расхода", "Продукты", 850),
])
@pytest.mark.parametrize("use_example", [False, True])
def test_selected_type_matches_prompts_and_saved_record_after_restart(
    tmp_path, monkeypatch, kind, button, label, category, example_amount, use_example,
):
    async def scenario():
        monkeypatch.setattr(guides, "render_guide", lambda topic: b"guide fixture")
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        await chat.text(button)
        prompt = last_text(chat)
        assert f"сумму {label}" in prompt
        assert category.casefold() in prompt
        opposite = "продукты" if kind == "income" else "зарплата"
        assert opposite not in prompt.casefold()
        assert [call.photo.filename for call in chat.session.calls if isinstance(call, SendPhoto)] == [f"guide-{kind}.png"]
        assert (await chat.state().get_data())["draft"]["kind"] == kind

        # Follow the example printed by the bot, or its separate amount step.
        example = prompt.partition("Можно сразу: ")[2]
        chat = await chat.restart(monkeypatch)
        await chat.text("не число")
        assert f"сумму {label}" in last_text(chat)
        assert opposite not in last_text(chat).casefold()
        assert (await chat.state().get_data())["draft"]["kind"] == kind
        await chat.text(example if use_example else "1250,50")
        if not use_example:
            assert f"категорию {label}" in last_text(chat)
            await chat.tap(chat.button(category))
        draft = (await chat.state().get_data())["draft"]
        assert draft["kind"] == kind
        assert draft["amount"] == (example_amount if use_example else 1250.50)

        # Editing an existing draft must keep both wording and the selected kind.
        await chat.tap(chat.button("Сумма"))
        assert f"сумму {label}" in last_text(chat)
        await chat.text("2000,75")
        save = chat.button("Сохранить")
        await chat.tap(save)
        await chat.tap(save)
        rows = await chat.db.transactions(1, today(chat.db.timezone).strftime("%Y-%m"))
        assert len(rows) == 1
        assert rows[0]["kind"] == kind
        assert rows[0]["amount_minor"] == 200075
        assert rows[0]["category"] == category.casefold()
        summary = await chat.db.summary(1)
        assert summary[kind] == 2000.75
        assert summary["expense" if kind == "income" else "income"] == 0
        await chat.close()

    asyncio.run(scenario())

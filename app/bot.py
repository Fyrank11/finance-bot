from __future__ import annotations

import asyncio
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from .config import load_settings
from .db import Database
from .finance import affordability, allocation, credit_card_advice, money
from .keyboards import MAIN_MENU

router = Router()
db: Database
allowed_user_ids: frozenset[int] = frozenset()


class Form(StatesGroup):
    income = State(); expense = State(); debt = State(); goal = State(); afford = State(); credit = State()


def parse_amount(value: str) -> float:
    cleaned = re.sub(r"[^0-9,.-]", "", value).replace(",", ".")
    amount = float(cleaned)
    if amount <= 0:
        raise ValueError
    return amount


async def permitted(message: Message) -> bool:
    if not message.from_user or (allowed_user_ids and message.from_user.id not in allowed_user_ids):
        await message.answer("У вас нет доступа к этому боту.")
        return False
    return True


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    if not await permitted(message): return
    await state.clear()
    await message.answer("Привет! Я веду личный бюджет и помогаю принимать решения о покупках. Выберите действие:", reply_markup=MAIN_MENU)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer("Используйте кнопки меню. Форматы ввода я показываю на каждом шаге. /cancel отменяет текущий ввод.", reply_markup=MAIN_MENU)


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear(); await message.answer("Отменено.", reply_markup=MAIN_MENU)


@router.message(F.text == "➕ Доход")
async def income_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.income); await message.answer("Введите: сумма категория [заметка]\nНапример: 120000 зарплата август")


@router.message(Form.income)
async def income_save(message: Message, state: FSMContext) -> None:
    await save_transaction(message, state, "income")


@router.message(F.text == "➖ Расход")
async def expense_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.expense); await message.answer("Введите: сумма категория [заметка]\nНапример: 2450 продукты супермаркет")


@router.message(Form.expense)
async def expense_save(message: Message, state: FSMContext) -> None:
    await save_transaction(message, state, "expense")


async def save_transaction(message: Message, state: FSMContext, kind: str) -> None:
    try:
        parts = (message.text or "").split(maxsplit=2); amount = parse_amount(parts[0]); category = parts[1]
        note = parts[2] if len(parts) > 2 else ""
    except (ValueError, IndexError):
        await message.answer("Не понял. Пример: 45000 зарплата или 1500 транспорт такси"); return
    await db.add_transaction(message.from_user.id, kind, category.lower(), amount, note)
    await state.clear(); await message.answer(f"Сохранено: {money(amount)} · {category}", reply_markup=MAIN_MENU)


@router.message(F.text == "🤝 Долг")
async def debt_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.debt); await message.answer("Введите: сумма направление название\nНаправление: должен или мне\nНапример: 30000 должен кредитка")


@router.message(Form.debt)
async def debt_save(message: Message, state: FSMContext) -> None:
    try:
        amount_s, direction_s, name = (message.text or "").split(maxsplit=2); amount = parse_amount(amount_s)
        direction = {"должен": "i_owe", "мне": "owed_to_me"}[direction_s.lower()]
    except (ValueError, KeyError):
        await message.answer("Пример: 30000 должен кредитка или 5000 мне Алексей"); return
    await db.add_debt(message.from_user.id, name, amount, direction)
    await state.clear(); await message.answer("Долг сохранён.", reply_markup=MAIN_MENU)


@router.message(F.text == "🎯 Цель")
async def goal_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.goal); await message.answer("Введите: сумма название\nНапример: 250000 отпуск")


@router.message(Form.goal)
async def goal_save(message: Message, state: FSMContext) -> None:
    try: amount_s, name = (message.text or "").split(maxsplit=1); amount = parse_amount(amount_s)
    except ValueError: await message.answer("Пример: 250000 отпуск"); return
    await db.add_goal(message.from_user.id, name, amount)
    await state.clear(); await message.answer("Цель сохранена.", reply_markup=MAIN_MENU)


def metrics(data: dict) -> tuple[float, float]:
    debt = data["debts"].get("i_owe", 0)
    goals_remaining = sum(max(goal["target"] - goal["saved"], 0) for goal in data["goals"])
    return debt, goals_remaining


@router.message(F.text == "📊 Сводка")
async def show_summary(message: Message) -> None:
    data = await db.summary(message.from_user.id); debt, remaining = metrics(data)
    cats = "\n".join(f"• {x['category']}: {money(x['total'])}" for x in data["categories"]) or "• пока нет"
    await message.answer(f"📊 {data['month']}\nДоходы: {money(data['income'])}\nРасходы: {money(data['expense'])}\nОстаток: {money(data['balance'])}\nДолги: {money(debt)}\nНа цели осталось: {money(remaining)}\n\nТоп расходов:\n{cats}")


@router.message(F.text == "🧭 Распределение")
async def show_allocation(message: Message) -> None:
    data = await db.summary(message.from_user.id); debt, remaining = metrics(data)
    await message.answer(allocation(data["income"], data["expense"], debt, remaining))


@router.message(F.text == "💬 Могу позволить?")
async def afford_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.afford); await message.answer("Сколько стоит покупка?")


@router.message(Form.afford)
async def afford_answer(message: Message, state: FSMContext) -> None:
    try: price = parse_amount(message.text or "")
    except ValueError: await message.answer("Введите сумму числом, например 15000"); return
    data = await db.summary(message.from_user.id); debt, remaining = metrics(data)
    await state.clear(); await message.answer(affordability(price, data["balance"], debt, remaining), reply_markup=MAIN_MENU)


@router.message(F.text == "💳 Кредитка?")
async def credit_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.credit); await message.answer("Введите сумму покупки.")


@router.message(Form.credit)
async def credit_answer(message: Message, state: FSMContext) -> None:
    try: price = parse_amount(message.text or "")
    except ValueError: await message.answer("Введите сумму числом, например 15000"); return
    data = await db.summary(message.from_user.id); debt, _ = metrics(data)
    await state.clear(); await message.answer(credit_card_advice(price, data["balance"], debt), reply_markup=MAIN_MENU)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Выберите действие кнопкой меню.", reply_markup=MAIN_MENU)


async def main() -> None:
    global db, allowed_user_ids
    settings = load_settings(); allowed_user_ids = settings.allowed_user_ids
    db = Database(settings.db_path); await db.init()
    dispatcher = Dispatcher(); dispatcher.include_router(router)
    await dispatcher.start_polling(Bot(settings.bot_token))


if __name__ == "__main__":
    asyncio.run(main())


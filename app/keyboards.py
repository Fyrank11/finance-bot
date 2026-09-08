from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

MAIN_MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="➕ Доход"), KeyboardButton(text="➖ Расход")],
    [KeyboardButton(text="📊 Мой бюджет"), KeyboardButton(text="🧭 Планы")],
    [KeyboardButton(text="📈 Аналитика"), KeyboardButton(text="⚙️ Настройки и помощь")],
], resize_keyboard=True, input_field_placeholder="Например: продукты 850")
CANCEL_MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="ℹ️ Как это работает"), KeyboardButton(text="❌ Отмена")],
], resize_keyboard=True)
EXTRA_MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🌱 Накопления"), KeyboardButton(text="🎯 Мои цели")],
    [KeyboardButton(text="🤝 Долг"), KeyboardButton(text="💬 Могу позволить?")],
    [KeyboardButton(text="💳 Кредитка?"), KeyboardButton(text="🏠 Меню")],
], resize_keyboard=True)

# Telegram can retain a previously delivered reply keyboard across releases.
LEGACY_MENU_TEXTS = frozenset({
    '📝 История', '🎯 Лимиты', '📁 Скачать Excel', '🗓 Платежи', '🌱 Накопления',
    '💡 Подсказки', '🔮 Прогноз', '📬 Обзор недели', '🔎 Поиск', '👥 Семья',
    '⚙️ Настройки', 'Ещё', '📊 Сводка', '🎯 Цель', '🧭 Распределение',
})


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
    ])


def categories(items: tuple[str, ...], prefix: str) -> InlineKeyboardMarkup:
    buttons = [(name.capitalize(), f"{prefix}:{i}") for i, name in enumerate(items)]
    return inline([buttons[i:i + 2] for i in range(0, len(buttons), 2)])

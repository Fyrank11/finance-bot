from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Доход"), KeyboardButton(text="➖ Расход")],
        [KeyboardButton(text="🤝 Долг"), KeyboardButton(text="🎯 Цель")],
        [KeyboardButton(text="📊 Сводка"), KeyboardButton(text="🧭 Распределение")],
        [KeyboardButton(text="💬 Могу позволить?"), KeyboardButton(text="💳 Кредитка?")],
        [KeyboardButton(text="❌ Отмена")],
    ], resize_keyboard=True, input_field_placeholder="Выберите действие"
)


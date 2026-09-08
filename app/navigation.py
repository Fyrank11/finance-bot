"""Six top-level buttons, with scoped section links and a graphical help index."""
from __future__ import annotations

from . import family
from .guide_catalog import GUIDE_CATALOG
from .keyboards import inline

# Explicit route whitelist: callback values never become module or file names.
TOPICS = {
    'summary': 'budget', 'history': 'history', 'limits': 'limits', 'export': 'export',
    'payments': 'payments', 'savings': 'savings', 'goals': 'goals', 'forecast': 'forecast',
    'weekly': 'weekly', 'analytics': 'analytics', 'tips': 'tips', 'search': 'search',
    'family': 'family', 'settings': 'settings', 'debts': 'debts', 'afford': 'afford',
    'credit': 'credit',
}
HUBS = {
    'plans': ('🧭 Планы и накопления', 'Выберите, что хотите запланировать. Кнопка ℹ️ рядом с разделом открывает инструкцию.', [
        ('🗓 Платежи', 'payments'), ('🌱 Накопления', 'savings'), ('🎯 Мои цели', 'goals'),
        ('🔮 Прогноз', 'forecast'), ('🤝 Долги', 'debts'), ('Дополнительные расчеты', 'calculators'),
    ]),
    'settings_help': ('⚙️ Настройки и помощь', 'Здесь — параметры учета, семейный бюджет и инструкции.', [
        ('📖 Инструкции к разделам', 'help'), ('🖼 Знакомство с ботом', 'welcome'),
        ('⚙️ Настройки бюджета', 'settings'), ('👥 Семья', 'family'), ('🔐 О данных', 'privacy'),
    ]),
    'calculators': ('Дополнительные расчеты', 'Упрощенные расчеты по записям. Они не проверяют условия банка и не подтверждают безопасность покупки.', [
        ('💬 Могу позволить?', 'afford'), ('💳 Кредитка?', 'credit'),
    ]),
}


async def section_rows(db, message, links):
    budget_id, revision = await family.active_budget_context(db, message.chat.id)
    rows = []
    for label, action in links:
        row = [(label, f'scope:{budget_id}:{revision}:nav:{action}')]
        if action in TOPICS:
            row.append(('ℹ️', f'guide:{TOPICS[action]}'))
        rows.append(row)
    return rows


async def show_hub(message, db, name):
    title, description, links = HUBS[name]
    rows = await section_rows(db, message, links)
    rows.append([('🏠 Главное меню', 'nav:begin')])
    await message.answer(f'{title}\n\n{description}', reply_markup=inline(rows))


async def show_help(message, db, page=0):
    topics = list(GUIDE_CATALOG)
    page = min(page, max((len(topics) - 1) // 6, 0))
    rows = [[(GUIDE_CATALOG[topic]['title'], f'guide:{topic}')] for topic in topics[page * 6:(page + 1) * 6]]
    nav = []
    if page:
        nav.append(('‹ Назад', f'nav:help:{page - 1}'))
    if (page + 1) * 6 < len(topics):
        nav.append(('Далее ›', f'nav:help:{page + 1}'))
    if nav:
        rows.append(nav)
    rows.append([('🖼 Знакомство с ботом', 'guide:welcome'), ('🏠 Меню', 'nav:begin')])
    await message.answer(
        f'📖 Инструкции · {page + 1}/{max((len(topics) + 5) // 6, 1)}\n\n'
        'Выберите раздел: покажу карточку и текст — зачем он нужен, что ввести и что получится. '
        'Открытие инструкции сохраняет незавершенный ввод. /cancel — отменить ввод.',
        reply_markup=inline(rows),
    )

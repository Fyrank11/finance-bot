"""Russian profile copy and onboarding, shared by Telegram and project docs."""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramAPIError

BOT_NAME = "Мой Финансовый Советник"
SHORT_DESCRIPTION = (
    "Личный и семейный бюджет: быстрый учёт, графики расходов и понятные подсказки."
)
DESCRIPTION = (
    "Деньги под контролем — без сложных таблиц.\n\n"
    "Записывайте траты сообщением: «кофе 350». "
    "Смотрите графики, задавайте лимиты и планируйте обязательные платежи. "
    "Рассчитывайте накопления на цели и резерв. "
    "Ведите личный бюджет или общий с семьёй.\n\n"
    "Подсказки помогут разобраться в расходах по вашим записям. "
    "Банковские счета автоматически не подключаются.\n\n"
    "Сейчас доступ по приглашению. Нажмите «Начать»."
)
PRIVACY_TEXT = (
    "🔐 О ваших данных\n\n"
    "На сервере сохраняются ваш Telegram ID, настройки и внесённые записи бюджета. "
    "Также сохраняются цели, резерв и подтвержденный план накоплений. "
    "Сообщения и отчёты передаются через Telegram. Администратор сервиса имеет технический доступ к базе.\n\n"
    "Другие пользователи не видят ваш личный бюджет. При вступлении в «Семью» "
    "участники видят и могут менять общие записи; личная история туда не переносится.\n\n"
    "Графики и подсказки рассчитываются на сервере без передачи записей внешней нейросети. "
    "Незавершенный ввод хранится до 30 дней после последнего изменения и переживает обновления бота. "
    "В истории можно исправлять и удалять отдельные операции, в меню — скачать Excel за выбранный месяц."
)


def profile_description(*, public_signup: bool = False) -> str:
    if public_signup:
        return DESCRIPTION.replace(
            "Сейчас доступ по приглашению. Нажмите «Начать».",
            "Нажмите «Начать» — личный бюджет станет доступен сразу. О данных: /privacy.",
        )
    return DESCRIPTION


def welcome_text(*, family_budget: bool = False) -> str:
    mode = "Семейный бюджет" if family_budget else "Личный бюджет"
    return (
        f"👋 {BOT_NAME}\n{mode}\n\n"
        "Записывайте деньги в пару касаний:\n"
        "кофе 350\n+ зарплата 150000\n\n"
        "1. Добавьте доход или расход — бот попросит подтверждение.\n"
        "2. Откройте «Мой бюджет», чтобы увидеть остаток.\n"
        "3. В «Аналитике» посмотрите графики и подсказки.\n\n"
        "🌱 «Накопления» — цели, резерв и выбранный вами план взносов.\n\n"
        "Начальные деньги — в настройках: это сумма перед первой записью.\n"
        "Личный бюджет виден только вам среди пользователей бота. "
        "Общий бюджет включается по отдельному приглашению.\n"
        "/privacy — хранение данных · /help — как пользоваться"
    )


async def apply_text_profile(bot, *, public_signup: bool = False) -> bool:
    """Update text only when this release starts. Profile failure must not stop polling."""
    try:
        for language in ("", "ru"):
            await bot.set_my_description(description=profile_description(public_signup=public_signup), language_code=language)
            await bot.set_my_short_description(
                short_description=SHORT_DESCRIPTION, language_code=language
            )
    except TelegramAPIError:
        logging.getLogger(__name__).warning(
            "Profile text could not be updated; continuing with the existing profile."
        )
        return False
    return True

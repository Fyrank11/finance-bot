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
    "Ведите личный бюджет или общий с семьёй.\n\n"
    "Подсказки помогут разобраться в расходах по вашим записям. "
    "Банковские счета автоматически не подключаются.\n\n"
    "Сейчас доступ по приглашению. Нажмите «Начать»."
)


def welcome_text(*, family_budget: bool = False) -> str:
    mode = "Семейный бюджет" if family_budget else "Личный бюджет"
    return (
        f"👋 {BOT_NAME}\n{mode}\n\n"
        "Записывайте деньги в пару касаний:\n"
        "кофе 350\n+ зарплата 150000\n\n"
        "1. Добавьте доход или расход — бот попросит подтверждение.\n"
        "2. Откройте «Мой бюджет», чтобы увидеть остаток.\n"
        "3. В «Аналитике» посмотрите графики и подсказки.\n\n"
        "Начальные деньги — в настройках: это сумма перед первой записью.\n"
        "/help — как пользоваться"
    )


async def apply_text_profile(bot) -> bool:
    """Update text only when this release starts. Profile failure must not stop polling."""
    try:
        for language in ("", "ru"):
            await bot.set_my_description(description=DESCRIPTION, language_code=language)
            await bot.set_my_short_description(
                short_description=SHORT_DESCRIPTION, language_code=language
            )
    except TelegramAPIError:
        logging.getLogger(__name__).warning(
            "Profile text could not be updated; continuing with the existing profile."
        )
        return False
    return True

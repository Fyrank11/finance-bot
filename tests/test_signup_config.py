from unittest.mock import AsyncMock

import asyncio
import pytest

from app import config
from app.branding import apply_text_profile, profile_description


@pytest.fixture
def settings_environment(monkeypatch):
    monkeypatch.setattr(config, 'load_dotenv', lambda: None)
    monkeypatch.setenv('BOT_TOKEN', '123456:UNIT_TEST_TOKEN_NOT_REAL')
    monkeypatch.setenv('ALLOWED_USER_IDS', '1, 2')
    monkeypatch.setenv('TIMEZONE', 'Europe/Moscow')
    monkeypatch.delenv('PUBLIC_SIGNUP', raising=False)
    return monkeypatch


def test_signup_is_closed_without_an_explicit_setting(settings_environment):
    settings = config.load_settings()
    assert settings.public_signup is False
    assert settings.allowed_user_ids == frozenset({1, 2})


@pytest.mark.parametrize(('value', 'expected'), [('true', True), ('false', False), (' TRUE ', True)])
def test_explicit_signup_setting_preserves_existing_allowlist(settings_environment, value, expected):
    settings_environment.setenv('PUBLIC_SIGNUP', value)
    settings = config.load_settings()
    assert settings.public_signup is expected
    assert settings.allowed_user_ids == frozenset({1, 2})


@pytest.mark.parametrize('value', ['yes', '1', '', 'notfalse'])
def test_misspelled_signup_setting_does_not_open_access(settings_environment, value):
    settings_environment.setenv('PUBLIC_SIGNUP', value)
    with pytest.raises(RuntimeError, match='PUBLIC_SIGNUP'):
        config.load_settings()


def test_profile_matches_actual_access_mode_and_api_length():
    async def run():
        for public in (True, False):
            bot = AsyncMock()
            assert await apply_text_profile(bot, public_signup=public)
            description = profile_description(public_signup=public)
            assert len(description) <= 512
            assert ('по приглашению' in description) is not public
            assert ('доступен сразу' in description) is public
            assert {call.kwargs['language_code'] for call in bot.set_my_description.call_args_list} == {'', 'ru'}
            assert all(call.kwargs['description'] == description for call in bot.set_my_description.call_args_list)
    asyncio.run(run())

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    db_path: Path
    allowed_user_ids: frozenset[int]


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and add the token.")
    raw_ids = os.getenv("ALLOWED_USER_IDS", "")
    try:
        allowed = frozenset(int(value.strip()) for value in raw_ids.split(",") if value.strip())
    except ValueError as exc:
        raise RuntimeError("ALLOWED_USER_IDS must contain comma-separated numbers") from exc
    return Settings(token, Path(os.getenv("DB_PATH", "data/finance.db")), allowed)


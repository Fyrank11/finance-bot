"""Preserve SQLite data before budget and savings schema upgrades."""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def backup_before_upgrade(db_path: Path, *, include_savings: bool = False) -> Path | None:
    """Back up an existing legacy database; any failure prevents migration."""
    db_path = db_path.resolve()
    if not db_path.exists():
        return None

    temporary: Path | None = None
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
        table = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()
        if not table:
            return None
        columns = {row[1] for row in source.execute("PRAGMA table_info(transactions)")}
        goals = {row[1] for row in source.execute("PRAGMA table_info(goals)")} if include_savings else set()
        savings_current = not include_savings or {'target_minor', 'saved_minor', 'monthly_minor', 'version'} <= goals
        if {"amount_minor", "version"} <= columns and savings_current:
            return None

        directory = db_path.parent / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        try:
            descriptor, filename = tempfile.mkstemp(
                prefix="finance-before-upgrade-", suffix=".tmp", dir=directory
            )
            temporary = Path(filename)
            os.close(descriptor)
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RuntimeError("Database backup verification failed; upgrade stopped")
            finished = temporary.with_suffix(".db")
            # A hard link publishes the verified copy atomically and never overwrites.
            os.link(temporary, finished)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    logging.getLogger(__name__).info("Database backup created: %s", finished)
    return finished

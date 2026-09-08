import sqlite3
from contextlib import closing

import pytest

from app.backup import backup_before_upgrade


def test_backup_includes_committed_wal_rows_and_preserves_source(tmp_path):
    path = tmp_path / "finance.db"
    with closing(sqlite3.connect(path)) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("CREATE TABLE transactions(id INTEGER PRIMARY KEY, amount REAL)")
        source.execute("INSERT INTO transactions VALUES(1, 12.34)")
        source.commit()
        assert (tmp_path / "finance.db-wal").stat().st_size > 0
        original = path.read_bytes()
        wal = (tmp_path / "finance.db-wal").read_bytes()

        copied = backup_before_upgrade(path)
        assert copied.parent == tmp_path / "backups"
        with closing(sqlite3.connect(copied)) as backup:
            assert backup.execute("SELECT * FROM transactions").fetchall() == [(1, 12.34)]
            assert backup.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert source.execute("SELECT * FROM transactions").fetchall() == [(1, 12.34)]
        assert path.read_bytes() == original
        assert (tmp_path / "finance.db-wal").read_bytes() == wal
        assert not list(copied.parent.glob("*.tmp"))

        # Repeated attempts preserve each previous recovery point.
        second = backup_before_upgrade(path)
        assert second != copied
        assert copied.exists() and second.exists()


@pytest.mark.parametrize("schema", [None, "empty", "current"])
def test_new_or_current_database_skips_backup(tmp_path, schema):
    path = tmp_path / "finance.db"
    if schema is not None:
        with closing(sqlite3.connect(path)) as source:
            if schema == "current":
                source.execute(
                    "CREATE TABLE transactions(id INTEGER, amount_minor INTEGER, version INTEGER)"
                )
            source.commit()
    assert backup_before_upgrade(path) is None
    assert not (tmp_path / "backups").exists()
    if schema is None:
        assert not path.exists()


def test_corrupt_database_stops_upgrade_without_changing_source(tmp_path):
    path = tmp_path / "finance.db"
    original = b"This is not a SQLite database."
    path.write_bytes(original)
    with pytest.raises(sqlite3.DatabaseError):
        backup_before_upgrade(path)
    assert path.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_savings_upgrade_backs_up_existing_goals_once(tmp_path):
    path = tmp_path / "finance.db"
    with closing(sqlite3.connect(path)) as source:
        source.execute("CREATE TABLE transactions(id INTEGER, amount_minor INTEGER, version INTEGER)")
        source.execute("CREATE TABLE goals(id INTEGER, name TEXT, target REAL, saved REAL)")
        source.execute("INSERT INTO goals VALUES(1, 'Reserve', 150000, 25000)")
        source.commit()

    copied = backup_before_upgrade(path, include_savings=True)
    assert copied is not None
    with closing(sqlite3.connect(copied)) as backup:
        assert backup.execute("SELECT * FROM goals").fetchall() == [(1, "Reserve", 150000, 25000)]
    with closing(sqlite3.connect(path)) as source:
        for column in ("target_minor", "saved_minor", "monthly_minor", "version"):
            source.execute(f"ALTER TABLE goals ADD COLUMN {column} INTEGER")
        source.commit()
    assert backup_before_upgrade(path, include_savings=True) is None
    assert len(list(copied.parent.glob("*.db"))) == 1

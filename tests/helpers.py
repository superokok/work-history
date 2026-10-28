"""Shared test utilities: temp databases and raw collector events."""

import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

from workhistory.db import Database


class TempDB:
    def __enter__(self) -> Database:
        self.dir = tempfile.mkdtemp(prefix="wh-test-")
        self.db = Database(Path(self.dir) / "test.db")
        self.db.migrate()
        return self.db

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


def local_ts(day: date, hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute, second).timestamp()


def add_raw(db: Database, kind: str, start: float, end: float, app: str = "", title: str = "",
            status: str = "") -> int:
    """A raw event as the collector writes it (kind 'window' or 'afk')."""
    with db.connect() as conn:
        return conn.execute("INSERT INTO collector_events (kind, start_ts, end_ts, app, title, status) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (kind, start, end, app, title, status)).lastrowid


def extend_raw(db: Database, raw_id: int, end: float) -> None:
    """The open event growing (a later heartbeat)."""
    with db.connect() as conn:
        conn.execute("UPDATE collector_events SET end_ts = ? WHERE id = ?", (end, raw_id))

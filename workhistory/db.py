"""SQLite access and schema migrations (tracked with PRAGMA user_version)."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Each entry upgrades the schema by one version. Never edit a released entry;
# append a new one instead.
MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE sources (
        id             INTEGER PRIMARY KEY,
        bucket_id      TEXT NOT NULL,
        bucket_created TEXT NOT NULL,
        kind           TEXT NOT NULL CHECK (kind IN ('window', 'afk')),
        hostname       TEXT NOT NULL,
        cursor_ts      REAL,
        last_sync_at   REAL,
        UNIQUE (bucket_id, bucket_created)
    );

    CREATE TABLE projects (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
        color       TEXT NOT NULL DEFAULT '#4f7cff',
        next_action TEXT NOT NULL DEFAULT '',
        archived    INTEGER NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL
    );

    CREATE TABLE window_events (
        id         INTEGER PRIMARY KEY,
        source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        aw_id      INTEGER NOT NULL,
        start_ts   REAL NOT NULL,
        end_ts     REAL NOT NULL,
        app        TEXT NOT NULL,
        title      TEXT NOT NULL,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        UNIQUE (source_id, aw_id)
    );
    CREATE INDEX idx_window_events_start ON window_events(start_ts);
    CREATE INDEX idx_window_events_end ON window_events(end_ts);

    CREATE TABLE afk_events (
        id        INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        aw_id     INTEGER NOT NULL,
        start_ts  REAL NOT NULL,
        end_ts    REAL NOT NULL,
        status    TEXT NOT NULL,
        UNIQUE (source_id, aw_id)
    );
    CREATE INDEX idx_afk_events_end ON afk_events(end_ts);

    CREATE TABLE project_rules (
        id         INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        field      TEXT NOT NULL CHECK (field IN ('app', 'title')),
        pattern    TEXT NOT NULL,
        created_at REAL NOT NULL
    );

    CREATE TABLE notes (
        id         INTEGER PRIMARY KEY,
        day        TEXT NOT NULL,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        body       TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX idx_notes_day ON notes(day);

    CREATE TABLE exclusions (
        id         INTEGER PRIMARY KEY,
        field      TEXT NOT NULL CHECK (field IN ('app', 'title')),
        pattern    TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (field, pattern)
    );

    -- Time ranges whose activity must not be imported or shown:
    -- 'pause' (end_ts NULL while paused) and 'deleted' (user deleted data).
    CREATE TABLE blocked_ranges (
        id       INTEGER PRIMARY KEY,
        kind     TEXT NOT NULL CHECK (kind IN ('pause', 'deleted')),
        start_ts REAL NOT NULL,
        end_ts   REAL
    );
    """,
    # v2: work sessions and daily reviews
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- Grouped work sessions. Auto fields (range, title, project) are recomputed
    -- unless the user edited them; `locked` freezes the range (merge/split).
    CREATE TABLE sessions (
        id             INTEGER PRIMARY KEY,
        day            TEXT NOT NULL,
        start_ts       REAL NOT NULL,
        end_ts         REAL NOT NULL,
        active_s       REAL NOT NULL DEFAULT 0,
        title          TEXT NOT NULL DEFAULT '',
        title_edited   INTEGER NOT NULL DEFAULT 0,
        project_id     INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        project_manual INTEGER NOT NULL DEFAULT 0,
        note           TEXT NOT NULL DEFAULT '',
        excluded       INTEGER NOT NULL DEFAULT 0,
        locked         INTEGER NOT NULL DEFAULT 0,
        created_at     REAL NOT NULL,
        updated_at     REAL NOT NULL
    );
    CREATE INDEX idx_sessions_day ON sessions(day, start_ts);
    CREATE INDEX idx_sessions_project ON sessions(project_id, end_ts);

    CREATE TABLE session_days (
        day     TEXT PRIMARY KEY,
        version INTEGER NOT NULL
    );
    INSERT OR IGNORE INTO session_days (day, version)
        SELECT DISTINCT date(start_ts, 'unixepoch', 'localtime'), -1 FROM window_events
        UNION SELECT DISTINCT date(end_ts, 'unixepoch', 'localtime'), -1 FROM window_events;

    CREATE TABLE daily_reviews (
        day        TEXT PRIMARY KEY,
        body       TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    """,
    # v3: record only selected work apps ("업무 앱만 기록")
    """
    CREATE TABLE track_rules (
        id            INTEGER PRIMARY KEY,
        app_pattern   TEXT NOT NULL DEFAULT '',
        title_pattern TEXT NOT NULL DEFAULT '',
        created_at    REAL NOT NULL,
        CHECK (app_pattern != '' OR title_pattern != '')
    );
    -- Part of a session's active time that is "업무 외" (untracked apps).
    ALTER TABLE sessions ADD COLUMN offwork_s REAL NOT NULL DEFAULT 0;
    """,
    # v4: AI reports and a content-free log of AI calls
    """
    CREATE TABLE reports (
        id         INTEGER PRIMARY KEY,
        kind       TEXT NOT NULL CHECK (kind IN ('daily', 'weekly')),
        start_day  TEXT NOT NULL,
        end_day    TEXT NOT NULL,
        body       TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (kind, start_day, end_day)
    );

    -- Never stores prompts or responses: only what is needed for cost/quality tracking.
    CREATE TABLE ai_runs (
        id              INTEGER PRIMARY KEY,
        kind            TEXT NOT NULL,
        model           TEXT NOT NULL,
        started_at      REAL NOT NULL,
        duration_ms     INTEGER NOT NULL,
        requests        INTEGER NOT NULL DEFAULT 0,
        input_tokens    INTEGER NOT NULL DEFAULT 0,
        output_tokens   INTEGER NOT NULL DEFAULT 0,
        thinking_tokens INTEGER NOT NULL DEFAULT 0,
        tool_calls      INTEGER NOT NULL DEFAULT 0,
        ok              INTEGER NOT NULL,
        error_kind      TEXT
    );
    """,
    # v5: results-based history: snapshots of work folders (see docs/RESULTS_TRACKING.md)
    """
    CREATE TABLE watched_folders (
        id           INTEGER PRIMARY KEY,
        path         TEXT NOT NULL UNIQUE COLLATE NOCASE,
        label        TEXT NOT NULL,
        project_id   INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        created_at   REAL NOT NULL,
        last_scan_at REAL,
        last_error   TEXT
    );

    -- Scan cache: a file is re-read only when its size or mtime changes.
    CREATE TABLE file_state (
        folder_id INTEGER NOT NULL REFERENCES watched_folders(id) ON DELETE CASCADE,
        path      TEXT NOT NULL,
        size      INTEGER NOT NULL,
        mtime_ns  INTEGER NOT NULL,
        blob_id   TEXT NOT NULL,
        PRIMARY KEY (folder_id, path)
    ) WITHOUT ROWID;

    -- One row per snapshot commit that differs from the previous one.
    CREATE TABLE changes (
        id             INTEGER PRIMARY KEY,
        folder_id      INTEGER NOT NULL REFERENCES watched_folders(id) ON DELETE CASCADE,
        commit_id      TEXT NOT NULL,
        ts             REAL NOT NULL,
        baseline       INTEGER NOT NULL DEFAULT 0,  -- first snapshot of a folder: not "work"
        files_added    INTEGER NOT NULL DEFAULT 0,
        files_modified INTEGER NOT NULL DEFAULT 0,
        files_deleted  INTEGER NOT NULL DEFAULT 0,
        lines_added    INTEGER NOT NULL DEFAULT 0,
        lines_removed  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_changes_ts ON changes(ts);

    CREATE TABLE change_files (
        id            INTEGER PRIMARY KEY,
        change_id     INTEGER NOT NULL REFERENCES changes(id) ON DELETE CASCADE,
        path          TEXT NOT NULL,
        kind          TEXT NOT NULL CHECK (kind IN ('A', 'M', 'D')),
        is_text       INTEGER NOT NULL,
        lines_added   INTEGER,
        lines_removed INTEGER,
        old_blob      TEXT,
        new_blob      TEXT
    );
    CREATE INDEX idx_change_files_change ON change_files(change_id);
    CREATE INDEX idx_change_files_path ON change_files(path);
    """,
    # v6: AI servers the user registered by address (OpenAI-compatible); model choice "remote:<id>"
    """
    CREATE TABLE ai_endpoints (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        url        TEXT NOT NULL,          -- API base ending in /v1
        model_id   TEXT NOT NULL,          -- the model name the server expects
        api_key    TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    );
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        # Safe with WAL: may lose the last commit on power loss, never corrupts.
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success and rolls back on error."""
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> int:
        conn = self._open()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema v{version} is newer than this app (v{SCHEMA_VERSION})."
                )
            for target in range(version + 1, SCHEMA_VERSION + 1):
                # executescript() commits first, so wrap each step explicitly.
                conn.executescript(
                    "BEGIN;\n" + MIGRATIONS[target - 1]
                    + f"\nPRAGMA user_version = {target};\nCOMMIT;"
                )
            return SCHEMA_VERSION
        finally:
            conn.close()

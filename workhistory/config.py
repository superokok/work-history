"""Application constants and default paths."""

import os
import sys
from pathlib import Path

APP_DIR_NAME = "WorkHistory"
DB_FILENAME = "workhistory.db"
DEMO_DB_FILENAME = "demo.db"

DEFAULT_PORT = 5730
DEFAULT_AW_URL = "http://127.0.0.1:5600"

SYNC_INTERVAL_S = 60
# While ActivityWatch is unreachable, retry this often instead.
RETRY_INTERVAL_S = 10
# Re-read this much before the cursor so late heartbeats are not missed.
SYNC_OVERLAP_S = 10 * 60
SYNC_CHUNK_S = 24 * 60 * 60
# How often registered work folders are snapshotted.
SNAPSHOT_INTERVAL_S = 5 * 60
DEFAULT_BACKFILL_DAYS = 30

# Foreground apps that mean "nobody is working" (Windows lock screen).
# Windows lock screen, macOS login window / screen saver (as reported by ActivityWatch).
IDLE_APPS = frozenset({"lockapp.exe", "loginwindow", "screensaverengine"})

# Consecutive activities with the same app/title/project closer than this are
# shown as one timeline row.
ROW_MERGE_GAP_S = 60

# Session grouping (see sessions.py). Constants on purpose: make them
# user-configurable only if real use shows the defaults don't fit.
SESSION_GAP_S = 10 * 60
MIN_PROJECT_RUN_S = 3 * 60      # switching to another known project
UNASSIGNED_RUN_S = 15 * 60      # unclassified work inside a project session
OFFWORK_RUN_S = 10 * 60         # 업무 외 inside a work session (like a short break)
MIN_KIND_RUN_S = 15 * 60        # coding / reading / chat must last this long to be its own session
MIN_MEETING_RUN_S = 5 * 60      # a meeting is its own session even when short

# Sessions shorter than this are left out of the daily review's main list.
REVIEW_MIN_SESSION_S = 5 * 60


def default_data_dir() -> Path:
    """Per-user application data folder, following each OS's convention."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_DIR_NAME

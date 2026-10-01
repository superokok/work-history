#!/bin/sh
# Starts ActivityWatch (if installed and not running) and the work-history app (macOS / Linux).
# Stop the app with Ctrl+C; ActivityWatch keeps running in the background.
cd "$(dirname "$0")" || exit 1

if ! pgrep -x aw-qt >/dev/null 2>&1 && ! pgrep -x aw-server >/dev/null 2>&1; then
  if [ -d "/Applications/ActivityWatch.app" ]; then
    open -a ActivityWatch
  elif command -v aw-qt >/dev/null 2>&1; then
    nohup aw-qt >/dev/null 2>&1 &
  else
    echo "ActivityWatch not found; window activity will not be recorded (folder snapshots still work)."
  fi
fi

# Prefer the project venv (it has the required packages), else the system Python.
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; fi
exec "$PY" -m workhistory "$@"

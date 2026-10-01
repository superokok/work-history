#!/bin/sh
# One-time setup on macOS / Linux: a private Python environment with the app's packages.
# Then start the app with ./start-workhistory.sh (and switch on autostart inside the app).
set -e
cd "$(dirname "$0")"

PY=""
# The first Python 3.11+ that can create virtual environments (Debian/Ubuntu ship venv separately).
for c in python3 python3.13 python3.12 python3.11; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer with venv is required."
  echo "  macOS: brew install python    Ubuntu/Debian: sudo apt install python3 python3-venv"
  exit 1
fi

echo "Using $($PY --version)"
"$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[claude]"
chmod +x start-workhistory.sh
echo
echo "Done. Start the app with:  ./start-workhistory.sh"
echo "For the AI features, install and sign in to Claude Code: https://docs.claude.com/en/docs/claude-code"

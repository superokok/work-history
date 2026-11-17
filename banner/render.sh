#!/bin/sh
# Renders banner.html to banner-800x1600.png with headless Edge.
cd "$(dirname "$0")"
E="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
W=$(cygpath -w "$PWD"); P="$(cygpath -w "$TEMP")\edge-banner"
U="file:///$(cygpath -m "$PWD")/banner.html"
"$E" --headless=new --disable-gpu --user-data-dir="$P" --hide-scrollbars --window-size=800,1600 --screenshot="$W\\banner-800x1600.png" "$U" 2>/dev/null
ls -la

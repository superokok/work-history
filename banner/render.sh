#!/bin/sh
# Renders banner.html to PNG (1x, 3x) and PDF with headless Edge.
cd "$(dirname "$0")"
E="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
W=$(cygpath -w "$PWD"); P="$(cygpath -w "$TEMP")\edge-banner"
U="file:///$(cygpath -m "$PWD")/banner.html"
"$E" --headless=new --disable-gpu --user-data-dir="$P" --hide-scrollbars --window-size=800,1600 --screenshot="$W\\banner-800x1600.png" "$U" 2>/dev/null
"$E" --headless=new --disable-gpu --user-data-dir="$P" --hide-scrollbars --window-size=800,1600 --force-device-scale-factor=3 --screenshot="$W\\banner-2400x4800.png" "$U" 2>/dev/null
"$E" --headless=new --disable-gpu --user-data-dir="$P" --no-pdf-header-footer --print-to-pdf="$W\\banner.pdf" "$U" 2>/dev/null
ls -la

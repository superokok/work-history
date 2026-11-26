#!/bin/sh
# Renders banner.html with headless Edge:
#   banner-800x1600.png        screen preview (800x1600 px)
#   banner-print.pdf / .ai     print file, 800x1600 mm, all text as vector outlines (no fonts needed)
# The print step needs a Python with fontTools and pypdf: PY=/path/to/python sh render.sh
cd "$(dirname "$0")"
E="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
W=$(cygpath -w "$PWD"); P="$(cygpath -w "$TEMP")\edge-banner"
U="file:///$(cygpath -m "$PWD")/banner.html"
"$E" --headless=new --disable-gpu --user-data-dir="$P" --hide-scrollbars --window-size=800,1600 --screenshot="$W\\banner-800x1600.png" "$U" 2>/dev/null
# A second profile: with the same one Edge hands the job to the still-closing first instance and no PDF comes out.
"$E" --headless=new --disable-gpu --user-data-dir="$P-pdf" --no-pdf-header-footer --print-to-pdf="$W\\_raw.pdf" "$U" 2>/dev/null
# Edge returns before the PDF is written: wait until the file is complete.
i=0; while [ $i -lt 30 ] && ! { [ -s _raw.pdf ] && tail -c 6 _raw.pdf | grep -q EOF; }; do sleep 1; i=$((i+1)); done
"${PY:-python}" outline_pdf.py _raw.pdf banner-print.pdf 800 1600 && cp banner-print.pdf banner-print.ai
rm -f _raw.pdf
ls -la

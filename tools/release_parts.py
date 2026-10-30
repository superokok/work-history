"""Split a big package zip into GitHub release-sized parts, plus a script that joins them.

GitHub takes release files up to 2 GiB each; the closed-network zip with the models is
about 8.5 GB. The parts are plain byte slices (`<zip>.001`, `.002`, ...), so Windows can
join them with its own `copy /b`; the join script then checks the SHA-256 with certutil.
Nothing has to be installed on the receiving PC.

Usage:
    python tools/release_parts.py dist/WorkHistory-<version>-windows-x64-offline-local-ai.zip
    -> dist/release/<zip>.001 ... and dist/release/WorkHistory-join.cmd
"""

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

PART_BYTES = 1_900_000_000  # under GitHub's 2 GiB per release file
CHUNK = 16 * 1024 * 1024
JOIN_NAME = "WorkHistory-join.cmd"  # ASCII: GitHub renames release files with other characters

JOIN_CMD = r"""@echo off
rem Joins the downloaded parts back into {zip} and checks it.
rem Put all {count} parts and this file in one folder, then double-click this file.
setlocal
cd /d "%~dp0"
set "ZIP={zip}"
set "EXPECTED={sha256}"
{checks}
echo Joining {count} parts into %ZIP% ... (a few minutes)
copy /b {parts} "%ZIP%" >nul
if errorlevel 1 (
  echo Joining failed. Is there enough free disk space?
  pause
  exit /b 1
)
echo Checking the file ...
set "HASH="
for /f "skip=1 delims=" %%h in ('certutil -hashfile "%ZIP%" SHA256') do if not defined HASH set "HASH=%%h"
set "HASH=%HASH: =%"
if /i not "%HASH%"=="%EXPECTED%" (
  echo The joined file is damaged. Download the parts again.
  del "%ZIP%"
  pause
  exit /b 1
)
echo OK: %ZIP%
echo Extract it to C:\ (right-click, Extract All, type C:\) and run C:\WorkHistory\*.cmd
echo The .001 ... .{last} parts can be deleted now.
pause
"""

CHECK_PART = """if not exist "%ZIP%.{n}" (
  echo Missing part: %ZIP%.{n}  - download all {count} parts into this folder.
  pause
  exit /b 1
)"""


def split(zip_path: Path, out: Path) -> tuple[list[Path], str]:
    out.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    parts: list[Path] = []
    with zip_path.open("rb") as src:
        while True:
            part = out / f"{zip_path.name}.{len(parts) + 1:03d}"
            written = 0
            with part.open("wb") as dst:
                while written < PART_BYTES:
                    block = src.read(min(CHUNK, PART_BYTES - written))
                    if not block:
                        break
                    digest.update(block)
                    dst.write(block)
                    written += len(block)
            if written == 0:
                part.unlink()
                break
            parts.append(part)
    return parts, digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("zip", type=Path)
    parser.add_argument("--out", type=Path, help="default: dist/release next to the zip")
    args = parser.parse_args(argv)
    out = args.out or args.zip.parent / "release"
    if out.exists():
        shutil.rmtree(out)
    parts, sha = split(args.zip, out)
    name, count = args.zip.name, len(parts)
    script = JOIN_CMD.format(
        zip=name, sha256=sha, count=count, last=f"{count:03d}",
        checks="\n".join(CHECK_PART.format(n=f"{i:03d}", count=count) for i in range(1, count + 1)),
        parts="+".join(f'"%ZIP%.{i:03d}"' for i in range(1, count + 1)))
    (out / JOIN_NAME).write_text(script.replace("\n", "\r\n"), encoding="ascii")
    print(f"{count} parts + {JOIN_NAME} in {out}  (SHA-256 {sha})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

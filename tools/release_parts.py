"""Make the GitHub release files: the app zip, the AI models in parts, and one setup script.

GitHub takes release files up to 2 GiB each, and the models are 2.7 GB (4B) and 5.7 GB (9B),
so each model is cut into plain byte slices (`<model>.gguf.001`, `.002`, ...). The app zip
(app, Python runtime, llama.cpp in `llm/bin`; built with `build_portable.py --offline`) stays
whole. `WorkHistory-setup.cmd` does the rest on the receiving PC with Windows' own tools,
nothing to install:

- unpacks the app zip next to itself (or updates C:\\WorkHistory, keeping its models),
- joins every model whose parts are all there into `WorkHistory\\llm\\models` with `copy /b`
  and checks its SHA-256 with certutil (models are optional: without one the AI stays off),
- offers to start the app.

Usage:
    python tools/build_portable.py --offline
    python tools/release_parts.py dist/WorkHistory-<version>-windows-x64-offline.zip
    -> dist/release/: the zip, <model>.gguf.001 ..., WorkHistory-setup.cmd
"""

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PART_BYTES = 1_900_000_000  # under GitHub's 2 GiB per release file
CHUNK = 16 * 1024 * 1024
SETUP_NAME = "WorkHistory-setup.cmd"  # ASCII: GitHub renames release files with other characters

# ASCII only (cmd reads the file in the console code page). Labels and gotos instead of
# parenthesized blocks, so messages may contain any punctuation.
SETUP_HEAD = r"""@echo off
rem WorkHistory setup: unpacks the app and joins the AI model parts. Double-click it.
rem Put it in one folder with the app zip and the model parts you downloaded.
setlocal EnableExtensions
cd /d "%~dp0"
set "APPZIP={zip}"
set "DEST=%CD%"
set "GOT="
set "INCOMPLETE="
if exist "%DEST%\WorkHistory\runtime\pythonw.exe" goto app
if not exist "C:\WorkHistory\runtime\pythonw.exe" goto app
if /i "%DEST%"=="C:\" goto app
if defined WH_UNATTENDED goto app
choice /c YN /m "WorkHistory is installed in C:\WorkHistory. Update that one"
if errorlevel 2 goto app
set "DEST=C:"

:app
set "APP=%DEST%\WorkHistory"
if exist "%APPZIP%" goto unpack
if exist "%APP%\runtime\pythonw.exe" goto models
echo Missing %APPZIP% - download it into this folder.
goto fail

:unpack
echo Unpacking the app into %APP% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%APPZIP%' -DestinationPath '%DEST%\' -Force"
if errorlevel 1 goto unpack_failed
goto models

:unpack_failed
echo Unpacking failed. If WorkHistory is running, quit it first: Settings - Run - Quit app.
goto fail

:models
if not exist "%APP%\llm\models" mkdir "%APP%\llm\models"
"""

SETUP_MODEL = r"""
if exist "%APP%\llm\models\{model}" goto have_{i}
if not exist "{model}.001" goto skip_{i}
{checks}
echo Joining {model} ... (a few minutes)
copy /b {parts} "%APP%\llm\models\{model}" >nul
if errorlevel 1 goto no_space
call :verify "%APP%\llm\models\{model}" {sha256}
if errorlevel 1 goto damaged_{i}
echo OK: {model}
set "GOT=1"
goto next_{i}
:damaged_{i}
echo {model} is damaged. Download its {count} parts again.
set "INCOMPLETE=1"
goto next_{i}
:missing_{i}
echo Some parts of {model} are missing: download all {count} ({model}.001 to .{last}).
set "INCOMPLETE=1"
goto next_{i}
:have_{i}
echo Already in place: {model}
set "GOT=1"
goto next_{i}
:skip_{i}
echo Not downloaded, optional: {model}
:next_{i}
"""

SETUP_TAIL = r"""
echo.
if defined INCOMPLETE echo Some models were not set up, see above. Run this file again after downloading.
if not defined GOT echo No AI model yet: the app works, AI reports stay off until a model is added.
echo Done: %APP%
echo To use it on another PC, copy the whole WorkHistory folder there, for example to C:\WorkHistory.
echo The downloaded .001 ... part files can be deleted now.
if defined WH_UNATTENDED exit /b 0
choice /c YN /m "Start WorkHistory now"
if errorlevel 2 exit /b 0
for %%f in ("%APP%\*.cmd") do start "" "%%f"
exit /b 0

:no_space
echo Joining failed. Is there enough free disk space?
goto fail

:fail
if not defined WH_UNATTENDED pause
exit /b 1

:verify
set "HASH="
for /f "skip=1 delims=" %%h in ('certutil -hashfile "%~1" SHA256') do if not defined HASH set "HASH=%%h"
set "HASH=%HASH: =%"
if /i "%HASH%"=="%~2" exit /b 0
del "%~1"
exit /b 1
"""

CHECK_PART = 'if not exist "{model}.{n}" goto missing_{i}'


def split(path: Path, out: Path, part_bytes: int = PART_BYTES) -> tuple[list[Path], str]:
    """Cut `path` into `<name>.001`, `.002`, ... in `out`; returns the parts and the file's SHA-256."""
    digest = hashlib.sha256()
    parts: list[Path] = []
    with path.open("rb") as src:
        while True:
            part = out / f"{path.name}.{len(parts) + 1:03d}"
            written = 0
            with part.open("wb") as dst:
                while written < part_bytes:
                    block = src.read(min(CHUNK, part_bytes - written))
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


def setup_script(app_zip: str, models: list[tuple[str, int, str]]) -> str:
    """The setup .cmd for `models` given as (file name, part count, SHA-256)."""
    blocks = []
    for i, (model, count, sha) in enumerate(models, 1):
        blocks.append(SETUP_MODEL.format(
            model=model, i=i, count=count, last=f"{count:03d}", sha256=sha,
            checks="\n".join(CHECK_PART.format(model=model, n=f"{n:03d}", i=i) for n in range(2, count + 1)),
            parts="+".join(f'"{model}.{n:03d}"' for n in range(1, count + 1))))
    return (SETUP_HEAD.format(zip=app_zip) + "".join(blocks) + SETUP_TAIL).replace("\n", "\r\n")


def make_release(app_zip: Path, models_dir: Path, out: Path, part_bytes: int = PART_BYTES) -> list[Path]:
    if not app_zip.name.isascii():
        raise SystemExit(f"{app_zip.name}: release file names must be ASCII")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy2(app_zip, out / app_zip.name)
    models = []
    for gguf in sorted(models_dir.glob("*.gguf")):
        if not gguf.name.isascii():
            raise SystemExit(f"{gguf.name}: release file names must be ASCII")
        parts, sha = split(gguf, out, part_bytes)
        models.append((gguf.name, len(parts), sha))
        print(f"{gguf.name}: {len(parts)} parts (SHA-256 {sha})")
    (out / SETUP_NAME).write_text(setup_script(app_zip.name, models), encoding="ascii")
    return sorted(out.iterdir())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("zip", type=Path, help="the app zip from build_portable.py --offline")
    parser.add_argument("--models", type=Path, default=ROOT / "llm" / "models", help="folder with the .gguf files")
    parser.add_argument("--out", type=Path, help="default: dist/release next to the zip")
    args = parser.parse_args(argv)
    files = make_release(args.zip, args.models, args.out or args.zip.parent / "release")
    print(f"{len(files)} release files in {files[0].parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

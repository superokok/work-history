"""Build the portable Windows package: dist/WorkHistory-<version>-windows-x64.zip

The user unzips it anywhere and double-clicks "작업 기록.cmd". Nothing is
installed system-wide and no Python is needed: the zip carries a private
Python runtime (copied from the interpreter the project venv was made with),
the app, and its runtime dependencies (copied from the venv: dulwich, mcp and
what they need). Nothing is downloaded.

Also writes THIRD_PARTY_NOTICES.md (in the zip and, with --notices, in the
repository) from the installed packages' own metadata and license files.

Usage (needs `packaging`, or pip which vendors it):
    python tools/build_portable.py            # build the zip
    python tools/build_portable.py --notices  # only refresh THIRD_PARTY_NOTICES.md
"""

import argparse
import importlib.metadata as md
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_PACKAGE = ROOT / "workhistory"
TOP_LEVEL = ["dulwich", "mcp"]          # pyproject: dependencies + the "claude" extra
LAUNCHER = "작업 기록.cmd"

# Standard-library parts the app never uses (GUI toolkits, tests, installers).
STDLIB_SKIP = {"test", "idlelib", "tkinter", "turtledemo", "ensurepip", "site-packages", "__pycache__"}
DLL_SKIP = re.compile(r"^(_tkinter|tcl\d+t?|tk\d+t?)\.", re.I)
# pywin32 extras nothing imports here: the Pythonwin IDE and the help file.
PACKAGE_SKIP = {"pythonwin", "PyWin32.chm"}
RUNTIME_FILES = re.compile(r"^(python(w)?\.exe|python3\d*\.dll|vcruntime140(_1)?\.dll|LICENSE\.txt)$", re.I)

LAUNCHER_CMD = r"""@echo off
rem WorkHistory launcher. Keep this file next to the "runtime" folder.
setlocal
cd /d "%~dp0"
if not exist "runtime\python.exe" (
  echo runtime\python.exe is missing. Extract the whole zip, then run this file from the extracted folder.
  pause
  exit /b 1
)
rem Re-open minimized: the app runs in that window, and closing it stops the app.
if not defined WORKHISTORY_MIN (
  set "WORKHISTORY_MIN=1"
  start "WorkHistory" /min cmd /c ""%~f0" %*"
  exit /b
)
set "WORKHISTORY_LAUNCHER=%~f0"
set "AW=%LOCALAPPDATA%\ActivityWatch\activitywatch\aw-qt.exe"
tasklist /FI "IMAGENAME eq aw-qt.exe" 2>nul | find /I "aw-qt.exe" >nul
if errorlevel 1 if exist "%AW%" start "" "%AW%"
rem -I: ignore PYTHON* variables and the user's own site-packages.
"runtime\python.exe" -I -m workhistory %*
if errorlevel 1 pause
"""

README_TXT = """작업 기록 (WorkHistory) {version}

시작
  1. 이 폴더를 원하는 곳(예: 문서 폴더)에 그대로 둡니다. 폴더 안 파일을 따로 옮기지 마세요.
     너무 깊은 폴더에 풀면 Windows 경로 길이 제한(260자)에 걸릴 수 있으니 짧은 경로를 쓰세요.
  2. "작업 기록.cmd"를 더블클릭하면 브라우저에 앱이 열립니다.
     작업 표시줄에 최소화된 "WorkHistory" 창이 앱 본체입니다. 그 창을 닫으면 앱이 종료됩니다.
  3. 앱의 개인정보 탭 → 실행에서 "컴퓨터를 켤 때 자동 실행", "바탕화면 바로가기"를 켤 수 있습니다.

선택 사항
  - 창 사용 기록: ActivityWatch를 설치하면 함께 기록합니다 (없어도 작업 폴더 기록은 동작).
    https://github.com/ActivityWatch/activitywatch/releases
  - AI 보고서·질문: Claude Code를 설치하고 로그인한 뒤, 개인정보 탭 → AI 설정에서 켭니다.
    https://docs.claude.com/en/docs/claude-code

데이터 위치
  %LOCALAPPDATA%\\WorkHistory  (이 폴더를 지우거나 새 버전으로 바꿔도 기록은 남습니다)

업데이트
  앱을 종료하고, 새 zip을 풀어 이 폴더를 통째로 바꾸면 됩니다.

라이선스: LICENSE.txt, 포함된 오픈소스: THIRD_PARTY_NOTICES.md
"""


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def venv_paths(venv: Path) -> tuple[Path, Path, str]:
    """(site-packages, base Python home, version) of a Windows venv."""
    cfg = dict(line.split("=", 1) for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
               if "=" in line)
    cfg = {k.strip(): v.strip() for k, v in cfg.items()}
    version = cfg.get("version_info") or cfg.get("version")
    return venv / "Lib" / "site-packages", Path(cfg["home"]), version


def closure(site: Path, names: list[str]) -> list[md.Distribution]:
    dists = {norm(d.metadata["Name"]): d for d in md.distributions(path=[str(site)])}
    found: dict[str, md.Distribution] = {}
    done: set[tuple[str, str]] = set()
    todo = [(norm(n), "") for n in names]  # (package, extra): "pyjwt[crypto]" also needs pyjwt's crypto deps
    while todo:
        name, extra = todo.pop()
        if (name, extra) in done:
            continue
        done.add((name, extra))
        if name not in dists:
            raise SystemExit(f"{name} is not installed in the venv: uv pip install -e .[claude]")
        found[name] = dists[name]
        for spec in dists[name].requires or []:
            req = Requirement(spec)
            if req.marker is None or req.marker.evaluate({"extra": extra}):
                todo += [(norm(req.name), e) for e in ("", *req.extras)]
    return sorted(found.values(), key=lambda d: norm(d.metadata["Name"]))


def license_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    return (meta.get("License") or "see license file").splitlines()[0][:80]


def license_files(dist: md.Distribution) -> list:
    return [f for f in dist.files or []
            if re.search(r"(licen[cs]e|copying|notice|authors)[^/]*$", str(f), re.I) and ".dist-info" in str(f)]


def homepage(dist: md.Distribution) -> str:
    meta = dist.metadata
    for url in meta.get_all("Project-URL") or []:
        label, _, link = url.partition(",")
        if label.strip().lower() in ("source", "source code", "repository", "homepage", "home"):
            return link.strip()
    return meta.get("Home-page") or ""


def notices(dists: list[md.Distribution], python_version: str) -> str:
    rows = ["# Third-party notices", "",
            "작업 기록(WorkHistory)은 아래 오픈소스 구성요소를 포함합니다. 모두 상업적 사용과 재배포를 허용하는",
            "라이선스이며, 각 라이선스 전문은 포터블 패키지의 `runtime/Lib/site-packages/<패키지>.dist-info/`",
            "(Python 자체는 `runtime/LICENSE.txt`)에 들어 있습니다.",
            "",
            "This product includes the open-source components below, all under licenses that permit commercial",
            "use and redistribution. Full license texts ship in the portable package under",
            "`runtime/Lib/site-packages/<package>.dist-info/` (Python itself: `runtime/LICENSE.txt`).",
            "",
            "| Component | Version | License | Source |",
            "|---|---|---|---|",
            f"| Python (CPython runtime) | {python_version} | PSF-2.0 (bundled OpenSSL, SQLite, libffi, zlib, "
            "bzip2, xz: see runtime/LICENSE.txt) | https://www.python.org |"]
    for d in dists:
        lic = license_of(d)
        if norm(d.metadata["Name"]) == "dulwich":
            lic += " — used under Apache-2.0"
        rows.append(f"| {d.metadata['Name']} | {d.version} | {lic} | {homepage(d)} |")
    rows += ["",
             "## Not included (installed separately by the user, optional)",
             "",
             "| Component | License | Use |",
             "|---|---|---|",
             "| ActivityWatch | MPL-2.0 | window activity source (talked to over its local REST API) |",
             "| Claude Code | Anthropic Commercial Terms / Consumer Terms | AI features, run as a separate program |",
             ""]
    return "\n".join(rows)


def copy_runtime(home: Path, out: Path) -> None:
    for f in home.iterdir():
        if f.is_file() and RUNTIME_FILES.match(f.name):
            shutil.copy2(f, out / f.name)
    (out / "DLLs").mkdir()
    for f in (home / "DLLs").iterdir():
        if f.is_file() and not DLL_SKIP.match(f.name):
            shutil.copy2(f, out / "DLLs" / f.name)
    shutil.copytree(home / "Lib", out / "Lib",
                    ignore=lambda d, names: [n for n in names if n in STDLIB_SKIP or n.endswith(".pyc")])
    (out / "Lib" / "site-packages").mkdir()


def copy_packages(site: Path, dists: list[md.Distribution], target: Path) -> None:
    for d in dists:
        for f in d.files or []:
            rel = Path(str(f))
            if rel.parts[0] == ".." or "__pycache__" in rel.parts or rel.suffix == ".pyc":
                continue  # console scripts live outside site-packages; bytecode is rebuilt
            if rel.parts[0] in PACKAGE_SKIP:
                continue
            src = site / rel
            if src.is_file():
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target / rel)
    shutil.copytree(APP_PACKAGE, target / "workhistory",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def smoke_test(out: Path) -> None:
    """Import everything with the bundled Python only (isolated from the build machine's packages)."""
    code = ("import sys, workhistory, workhistory.server, workhistory.mcp_server, dulwich, mcp; "
            "assert sys.prefix.lower().startswith(sys.argv[1].lower()), sys.prefix; print(workhistory.__version__)")
    result = subprocess.run([str(out / "runtime" / "python.exe"), "-I", "-c", code, str(out / "runtime")],
                            capture_output=True, text=True, cwd=str(out), timeout=120)
    if result.returncode != 0:
        raise SystemExit("smoke test failed:\n" + result.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv")
    parser.add_argument("--notices", action="store_true", help="only write THIRD_PARTY_NOTICES.md in the repository")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        raise SystemExit("The portable package is for Windows; on macOS / Linux use install.sh.")

    site, home, py_version = venv_paths(args.venv)
    dists = closure(site, TOP_LEVEL)
    text = notices(dists, py_version)
    (ROOT / "THIRD_PARTY_NOTICES.md").write_text(text, encoding="utf-8")
    print(f"THIRD_PARTY_NOTICES.md: {len(dists)} packages")
    if args.notices:
        return 0

    sys.path.insert(0, str(ROOT))
    from workhistory import __version__
    out = DIST / "WorkHistory"
    if out.exists():
        shutil.rmtree(out)
    (out / "runtime").mkdir(parents=True)
    copy_runtime(home, out / "runtime")
    copy_packages(site, dists, out / "runtime" / "Lib" / "site-packages")
    (out / LAUNCHER).write_text(LAUNCHER_CMD.replace("\n", "\r\n"), encoding="ascii")
    (out / "LICENSE.txt").write_text((ROOT / "LICENSE").read_text(encoding="utf-8"), encoding="utf-8-sig")
    (out / "THIRD_PARTY_NOTICES.md").write_text(text, encoding="utf-8")
    (out / "사용법.txt").write_text(README_TXT.format(version=__version__), encoding="utf-8-sig")
    smoke_test(out)

    zip_path = DIST / f"WorkHistory-{__version__}-windows-x64.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, Path("WorkHistory") / f.relative_to(out))
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"{zip_path.name}: {zip_path.stat().st_size / 1e6:.1f} MB (unpacked {size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

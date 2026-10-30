"""Build the portable Windows package: dist/WorkHistory-<version>-windows-x64.zip

The user unzips it anywhere and double-clicks "작업 기록.cmd". Nothing is
installed system-wide and no Python is needed: the zip carries a private
Python runtime (copied from the interpreter the project venv was made with),
the app, and its runtime dependencies (copied from the venv: dulwich, mcp and
what they need). Nothing is downloaded.

If the repository's `llm/` folder holds llama.cpp (`llm/bin`, see
workhistory/ai_local.py), the zip also carries `llm/bin` (llama-server and its
DLLs only) and an empty `llm/models` with instructions: model files are
handed out separately and dropped in there. `--with-models` puts the
`llm/models/*.gguf` files into the zip as well (a multi-GB zip).

`--offline` builds the package for a closed network: it leaves out the Claude
Code link (the `mcp` package and the ~30 libraries it pulls in), which needs
the internet anyway; the app then offers only the local AI.

Also writes THIRD_PARTY_NOTICES.md (in the zip and, with --notices, in the
repository) from the installed packages' own metadata and license files.

Usage (needs `packaging`, or pip which vendors it):
    python tools/build_portable.py            # build the zip
    python tools/build_portable.py --notices  # only refresh THIRD_PARTY_NOTICES.md
    python tools/build_portable.py --with-models  # also put the local AI models into the zip
    python tools/build_portable.py --offline --with-models  # closed network: no Claude Code link
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
OFFLINE_TOP_LEVEL = ["dulwich"]         # --offline: no Claude Code link
LAUNCHER = "작업 기록.cmd"
LLM = ROOT / "llm"
# llama-server and what it loads; the other llama.cpp tools stay out.
LLM_FILES = re.compile(r"^(llama-server\.exe|llama-server-impl\.dll|llama\.dll|llama-common\.dll|mtmd\.dll|"
                       r"libomp\.dll|ggml.*\.dll|LICENSE.*)$", re.I)
LLM_LICENSES = ("LICENSE-llama.cpp.txt", "LICENSE-Qwen3.5.txt")
MODELS_README = """로컬 AI 모델 넣는 곳

여기에 .gguf 모델 파일을 넣으면 앱의 개인정보 → AI 설정에 "내 PC · <파일 이름>"으로 나타납니다.
모델 파일은 앱과 따로 받습니다 (수 GB). 기록은 이 PC 밖으로 나가지 않습니다.

Put .gguf model files here; each appears in Privacy → AI settings as "내 PC · <file name>".
"""

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
set "WORKHISTORY_LAUNCHER=%~f0"
rem Runs in the background (pythonw: no console window to close by mistake); errors show in a message box.
rem -I: ignore PYTHON* variables and the user's own site-packages.
start "" "runtime\pythonw.exe" -I -m workhistory %*
"""

README_TXT = """작업 기록 (WorkHistory) {version}

시작
  1. 이 폴더를 원하는 곳(예: 문서 폴더)에 그대로 둡니다. 폴더 안 파일을 따로 옮기지 마세요.
     너무 깊은 폴더에 풀면 Windows 경로 길이 제한(260자)에 걸릴 수 있으니 짧은 경로를 쓰세요.
  2. "작업 기록.cmd"를 더블클릭하면 브라우저에 앱이 열립니다.
     앱은 창 없이 뒤에서 기록합니다. 브라우저 탭을 닫아도 기록은 계속됩니다.
     끄려면 앱의 개인정보 탭 → 실행 → "앱 종료"를 누르세요.
  3. 앱의 개인정보 탭 → 실행에서 "컴퓨터를 켤 때 자동 실행", "바탕화면 바로가기"를 켤 수 있습니다.

선택 사항
  - AI 보고서·질문 (둘 중 하나를 개인정보 탭 → AI 설정에서 고릅니다)
    · 내 PC의 AI: llm\\models 폴더에 .gguf 모델 파일을 넣으면 "내 PC · <모델>"이 나타납니다.
      기록이 PC 밖으로 나가지 않고 인터넷 없이 동작합니다. 그래픽카드가 없으면 느립니다.
{claude}
데이터 위치
  %LOCALAPPDATA%\\WorkHistory  (이 폴더를 지우거나 새 버전으로 바꿔도 기록은 남습니다)

업데이트
  앱을 종료하고, 새 zip을 풀어 이 폴더를 통째로 바꾸면 됩니다.

라이선스: LICENSE.txt, 포함된 오픈소스: THIRD_PARTY_NOTICES.md
"""
CLAUDE_TXT = """    · Claude Code: 설치하고 로그인하면 "Claude Code · Sonnet" 등이 나타납니다 (빠름, 외부 전송).
      https://docs.claude.com/en/docs/claude-code
"""
OFFLINE_TXT = """    · 이 패키지는 폐쇄망용이라 Claude Code 연결이 빠져 있습니다. 내 PC의 AI만 쓸 수 있습니다.
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
             "| Claude Code | Anthropic Commercial Terms / Consumer Terms | AI features, run as a separate program |",
             "",
             "## Local AI (portable package, `llm/` folder)",
             "",
             "| Component | License | Use | License text |",
             "|---|---|---|---|",
             "| llama.cpp / ggml (`llm/bin`) | MIT | runs the local model on this PC | `llm/LICENSE-llama.cpp.txt` |",
             "| LLVM OpenMP runtime (`llm/bin/libomp.dll`) | Apache-2.0 WITH LLVM-exception | used by llama.cpp | "
             "`llm/bin/LICENSE-LLVM-OpenMP` |",
             "| Qwen3.5 models (`llm/models`, if included) | Apache-2.0 | local AI model | `llm/LICENSE-Qwen3.5.txt` |",
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


def copy_llm(out: Path, with_models: bool) -> bool:
    """llm/bin (llama-server only), the license texts, and llm/models (empty unless with_models)."""
    bin_dir = LLM / "bin"
    if not (bin_dir / "llama-server.exe").is_file():
        if with_models:
            raise SystemExit("--with-models needs llm/bin/llama-server.exe (see workhistory/ai_local.py)")
        print("llm/bin/llama-server.exe not found: building without the local AI")
        return False
    (out / "llm" / "bin").mkdir(parents=True)
    for f in bin_dir.iterdir():
        if f.is_file() and LLM_FILES.match(f.name):
            shutil.copy2(f, out / "llm" / "bin" / f.name)
    for name in LLM_LICENSES:
        if (LLM / name).is_file():
            shutil.copy2(LLM / name, out / "llm" / name)
    models = out / "llm" / "models"
    models.mkdir()
    (models / "모델 넣는 곳.txt").write_text(MODELS_README, encoding="utf-8-sig")
    if with_models:
        ggufs = sorted((LLM / "models").glob("*.gguf"))
        if not ggufs:
            raise SystemExit("--with-models: no llm/models/*.gguf")
        for f in ggufs:
            shutil.copy2(f, models / f.name)
    return True


def smoke_test(out: Path, offline: bool) -> None:
    """Import everything with the bundled Python only (isolated from the build machine's packages)."""
    imports = "workhistory.ai_service" if offline else "workhistory.mcp_server, mcp"
    check = "assert not workhistory.ai_claude.bundled(); " if offline else ""
    code = (f"import sys, workhistory, workhistory.server, dulwich, {imports}; {check}"
            "assert sys.prefix.lower().startswith(sys.argv[1].lower()), sys.prefix; print(workhistory.__version__)")
    result = subprocess.run([str(out / "runtime" / "python.exe"), "-I", "-c", code, str(out / "runtime")],
                            capture_output=True, text=True, cwd=str(out), timeout=120)
    if result.returncode != 0:
        raise SystemExit("smoke test failed:\n" + result.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv")
    parser.add_argument("--notices", action="store_true", help="only write THIRD_PARTY_NOTICES.md in the repository")
    parser.add_argument("--with-models", action="store_true", help="put llm/models/*.gguf into the zip too")
    parser.add_argument("--offline", action="store_true", help="closed network: leave out the Claude Code link")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        raise SystemExit("The portable package is built on Windows (the app is Windows only).")

    site, home, py_version = venv_paths(args.venv)
    dists = closure(site, TOP_LEVEL)
    text = notices(dists, py_version)
    (ROOT / "THIRD_PARTY_NOTICES.md").write_text(text, encoding="utf-8")  # the repository lists the full set
    print(f"THIRD_PARTY_NOTICES.md: {len(dists)} packages")
    if args.notices:
        return 0
    if args.offline:
        dists = closure(site, OFFLINE_TOP_LEVEL)
        text = notices(dists, py_version)
        print(f"offline package: {len(dists)} packages")

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
    (out / "사용법.txt").write_text(README_TXT.format(version=__version__,
                                                   claude=OFFLINE_TXT if args.offline else CLAUDE_TXT),
                                    encoding="utf-8-sig")
    copy_llm(out, args.with_models)
    smoke_test(out, args.offline)

    suffix = ("-offline" if args.offline else "") + ("-local-ai" if args.with_models else "")
    zip_path = DIST / f"WorkHistory-{__version__}-windows-x64{suffix}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                # model files do not compress; storing them saves minutes
                z.write(f, Path("WorkHistory") / f.relative_to(out),
                        compress_type=zipfile.ZIP_STORED if f.suffix == ".gguf" else zipfile.ZIP_DEFLATED)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"{zip_path.name}: {zip_path.stat().st_size / 1e6:.1f} MB (unpacked {size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

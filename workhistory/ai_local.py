"""A language model running on this PC as the app's AI backend (llama.cpp `llama-server`).

Nothing leaves the PC. The app starts `llama-server` from its own `llm` folder,
bound to 127.0.0.1 on a free port with a random API key (other programs and web
pages on this PC cannot use it), talks to its OpenAI-compatible API, and stops
it after a while without use. Every `llm/models/*.gguf` file is one model
choice, named "local:<file name without .gguf>".

Folder layout (next to the app; WORKHISTORY_LLM_DIR overrides it):
    llm/bin/llama-server(.exe) + its DLLs
    llm/models/<model>.gguf
"""

import atexit
import logging
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .ai_client import AIError
from .ai_openai import HTTPFailure, OpenAICompatModel, http_json

log = logging.getLogger(__name__)

PREFIX = "local"
SERVER_EXE = "llama-server.exe" if os.name == "nt" else "llama-server"
CONTEXT_TOKENS = 32768        # the largest weekly context is ~17k tokens, plus the answer and tool turns
START_TIMEOUT_S = 300         # loading a 5 GB model from a slow disk
REQUEST_TIMEOUT_S = 1800      # a CPU-only PC can need minutes for one report
IDLE_STOP_S = 600             # give the memory back after 10 minutes without use
DEVICE_ENV = "WORKHISTORY_LLM_DEVICE"  # e.g. "none" = CPU only, "Vulkan0" = one GPU (measurements)

# No system proxy: the server is on this PC, and a proxy must never see the records.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_local_model(name: str) -> bool:
    return name.startswith(PREFIX + ":")


def llm_dir() -> Optional[Path]:
    """The `llm` folder: WORKHISTORY_LLM_DIR, else the nearest one above the app package
    (the repository in development; the unzipped WorkHistory folder in the portable package)."""
    env = os.environ.get("WORKHISTORY_LLM_DIR")
    if env:
        return Path(env)
    for parent in list(Path(__file__).resolve().parents)[:5]:
        if (parent / "llm" / "models").is_dir():
            return parent / "llm"
    return None


def server_exe(base: Optional[Path] = None) -> Optional[Path]:
    base = base or llm_dir()
    if base is None:
        return None
    for p in (base / "bin" / SERVER_EXE, base / SERVER_EXE):
        if p.is_file():
            return p
    return None


def model_files(base: Optional[Path] = None) -> dict[str, Path]:
    """Model choice name -> .gguf file; empty unless the server program is there too."""
    base = base or llm_dir()
    if base is None or server_exe(base) is None:
        return {}
    files = sorted(p for p in (base / "models").glob("*.gguf") if not p.name.lower().startswith("mmproj"))
    return {f"{PREFIX}:{p.stem}": p for p in files}


def available_models() -> list[str]:
    return list(model_files())


# ---------------------------------------------------------------- the server process

class _Server:
    """One llama-server at a time (one model in memory), started on demand, stopped when idle."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.model: Optional[Path] = None
        self.url = ""
        self.key = ""
        self.log_path: Optional[Path] = None
        self.last_used = 0.0
        self.busy = 0
        self._timer: Optional[threading.Timer] = None

    def acquire(self, exe: Path, model: Path) -> tuple[str, str]:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None or self.model != model:
                self._stop()
                self._start(exe, model)
            self.busy += 1
            return self.url, self.key

    def release(self) -> None:
        with self.lock:
            self.busy = max(self.busy - 1, 0)
            self.last_used = time.time()
            self._schedule_idle_stop()

    def _start(self, exe: Path, model: Path) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        key = secrets.token_urlsafe(24)
        args = [str(exe), "-m", str(model), "--host", "127.0.0.1", "--port", str(port), "--api-key", key,
                "-c", str(CONTEXT_TOKENS), "-np", "1", "--no-webui", "--jinja", "--no-mmproj"]
        device = os.environ.get(DEVICE_ENV)
        if device:
            args += ["-dev", device]
        fd, log_name = tempfile.mkstemp(prefix="workhistory-llm-", suffix=".log")
        os.close(fd)
        self.log_path = Path(log_name)
        with open(self.log_path, "wb") as out:
            try:
                self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                             cwd=str(exe.parent),
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                raise AIError(f"로컬 AI 실행기를 시작할 수 없습니다 ({type(e).__name__}).", "config") from None
        self.model, self.url, self.key = model, f"http://127.0.0.1:{port}", key
        deadline = time.time() + START_TIMEOUT_S
        while time.time() < deadline:
            if self.proc.poll() is not None:
                tail = self._log_tail()
                self._stop()
                raise AIError("로컬 AI 실행기가 시작하지 못했습니다" + (f": {tail}" if tail else "."), "config")
            try:
                with _opener.open(self.url + "/health", timeout=5) as r:
                    if r.status == 200:
                        log.info("local LLM ready: %s", model.name)
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)
        self._stop()
        raise AIError("로컬 AI 모델을 불러오는 데 너무 오래 걸립니다. 더 작은 모델을 고르세요.", "server")

    def _log_tail(self) -> str:
        try:
            lines = [ln for ln in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except OSError:
            return ""
        errors = [ln for ln in lines if "error" in ln.lower() or "failed" in ln.lower()]
        return (errors or lines or [""])[-1].strip()[:200]

    def _schedule_idle_stop(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(IDLE_STOP_S + 1, self._stop_if_idle)
        self._timer.daemon = True
        self._timer.start()

    def _stop_if_idle(self) -> None:
        with self.lock:
            if self.busy == 0 and time.time() - self.last_used >= IDLE_STOP_S:
                self._stop()

    def _stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc, self.model, self.url, self.key = None, None, "", ""
        if self.log_path:
            self.log_path.unlink(missing_ok=True)
            self.log_path = None

    def stop(self) -> None:
        with self.lock:
            if self._timer:
                self._timer.cancel()
            self._stop()


SERVER = _Server()
atexit.register(SERVER.stop)


# ---------------------------------------------------------------- the Model

class LocalModel(OpenAICompatModel):
    """`Model` backed by llama-server on this PC (requests and answers: ai_openai)."""

    def __init__(self, name: str, server: _Server = SERVER):
        files = model_files()
        if name not in files:
            raise AIError("로컬 AI 모델 파일을 찾을 수 없습니다. llm/models 폴더를 확인하세요.", "config")
        self.name = name
        self.path = files[name]
        self.model_id = self.path.stem
        self.exe = server_exe()
        self.server = server

    def _post(self, body: dict) -> dict:
        url, key = self.server.acquire(self.exe, self.path)
        try:
            return http_json(url + "/v1/chat/completions", body, key, REQUEST_TIMEOUT_S)
        except HTTPFailure:
            raise
        except AIError:
            raise AIError("로컬 AI가 제시간에 답하지 않았습니다. 기간을 줄이거나 더 작은 모델을 고르세요.", "server") from None
        finally:
            self.server.release()

    def check(self) -> None:
        """The program and model are here and the model loads. Sends no work data."""
        self.server.acquire(self.exe, self.path)
        self.server.release()

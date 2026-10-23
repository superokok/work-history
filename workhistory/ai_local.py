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
import json
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
from typing import Any, Callable, Optional

from .ai_client import MAX_TOOL_CALLS, AIError, AgentResult, ToolSpec, ToolTrace, Usage

log = logging.getLogger(__name__)

PREFIX = "local"
SERVER_EXE = "llama-server.exe" if os.name == "nt" else "llama-server"
CONTEXT_TOKENS = 32768        # the largest weekly context is ~17k tokens, plus the answer and tool turns
MAX_ANSWER_TOKENS = 8192
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

class LocalModel:
    """`Model` backed by llama-server on this PC. Reports: a JSON-schema-constrained answer.
    Questions: the app runs the tool loop itself (`run_tools`), so the model only ever sees
    what the app's read-only history tools return."""

    def __init__(self, name: str, server: _Server = SERVER):
        files = model_files()
        if name not in files:
            raise AIError("로컬 AI 모델 파일을 찾을 수 없습니다. llm/models 폴더를 확인하세요.", "config")
        self.name = name
        self.path = files[name]
        self.exe = server_exe()
        self.server = server

    def _post(self, body: dict) -> dict:
        url, key = self.server.acquire(self.exe, self.path)
        try:
            req = urllib.request.Request(url + "/v1/chat/completions", method="POST",
                                         data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            with _opener.open(req, timeout=REQUEST_TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if "context" in detail.lower() and ("exceed" in detail.lower() or "too long" in detail.lower()):
                raise AIError("기록이 많아 로컬 AI가 한 번에 읽을 수 없습니다. 기간을 줄여 다시 시도하세요.", "invalid_output") from None
            raise AIError(f"로컬 AI가 요청을 처리하지 못했습니다 (HTTP {e.code}).", "server") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise AIError("로컬 AI가 제시간에 답하지 않았습니다. 기간을 줄이거나 더 작은 모델을 고르세요.", "server") from None
        finally:
            self.server.release()

    def _chat(self, messages: list[dict], **extra: Any) -> tuple[dict, Usage]:
        body = {"model": self.path.stem, "messages": messages, "temperature": 0.2, "max_tokens": MAX_ANSWER_TOKENS,
                # thinking aloud costs minutes on a CPU and the app checks the answer anyway
                "chat_template_kwargs": {"enable_thinking": False}, **extra}
        data = self._post(body)
        u = data.get("usage") or {}
        usage = Usage(input_tokens=u.get("prompt_tokens") or 0, output_tokens=u.get("completion_tokens") or 0,
                      requests=1)
        try:
            return data["choices"][0]["message"], usage
        except (KeyError, IndexError, TypeError):
            raise AIError("로컬 AI 응답 형식이 올바르지 않습니다. 다시 시도하세요.", "invalid_output") from None

    def check(self) -> None:
        """The program and model are here and the model loads. Sends no work data."""
        self.server.acquire(self.exe, self.path)
        self.server.release()

    def generate_json(self, system: str, prompt: str, schema: dict,
                      max_output_tokens: int = 16000, thinking: Optional[str] = None) -> tuple[Any, Usage]:
        message, usage = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            max_tokens=min(max_output_tokens, MAX_ANSWER_TOKENS),
            response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}})
        try:
            return json.loads(message.get("content") or ""), usage
        except json.JSONDecodeError:
            raise AIError("로컬 AI 응답 형식이 올바르지 않습니다. 다시 시도하세요.", "invalid_output") from None

    def run_tools(self, system: str, prompt: str, tools: list[ToolSpec],
                  execute: Callable[[str, dict], Any], max_calls: int = MAX_TOOL_CALLS) -> AgentResult:
        specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": t.parameters}} for t in tools]
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        total, calls = Usage(), []
        while True:
            budget_left = len(calls) < max_calls
            message, usage = self._chat(messages, **({"tools": specs} if budget_left else {}))
            total.add(usage)
            requested = message.get("tool_calls") or []
            if not requested or not budget_left:
                return AgentResult((message.get("content") or "").strip(), total, calls)
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": requested})
            for call in requested:
                fn = call.get("function") or {}
                name, args = fn.get("name", ""), {}
                try:
                    parsed = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(parsed, dict):
                        raise ValueError("arguments must be an object")
                    args = parsed
                    result, ok = execute(name, args), True
                except Exception as e:  # a bad call is reported back to the model, as the MCP server does
                    result, ok = {"error": str(e)[:300]}, False
                calls.append(ToolTrace(name, args, ok))
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps(result, ensure_ascii=False)})

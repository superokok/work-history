"""Claude Code as the app's AI backend (`claude -p`, headless).

Uses the Claude Code installed and signed in on this PC, so the app needs no
API key. Every run is locked down: no built-in tools (no files, no shell, no
web), no MCP servers except the ones the app passes, no user or project
settings or hooks, no saved session, and an empty working directory. The only
thing Claude can do is answer, or read the work history through this app's
own read-only MCP server.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from .ai_client import AIError, Usage

PREFIX = "claude-code"
ALIASES = ("sonnet", "opus", "haiku")
MODELS = (PREFIX,) + tuple(f"{PREFIX}:{a}" for a in ALIASES)
TIMEOUT_S = 600
MAX_BUDGET_USD = "5"  # per run; a guard against runaway loops, far above a normal report

Runner = Callable[..., subprocess.CompletedProcess]


def bundled() -> bool:
    """Is the Claude Code link part of this install? The offline (closed-network) package leaves
    out the `mcp` package that "AI에게 묻기" needs, and with it every Claude Code model."""
    return importlib.util.find_spec("mcp") is not None


def is_shim(cli: str) -> bool:
    return Path(cli).suffix.lower() in (".cmd", ".bat")


def find_cli() -> Optional[str]:
    """The `claude` executable. For an npm install on Windows, PATH has a .cmd shim that
    starts the bundled native binary; run that binary directly so arguments (prompts,
    JSON) are passed verbatim instead of being re-parsed by cmd.exe."""
    found = shutil.which("claude")
    if found and is_shim(found):
        native = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.is_file():
            return str(native)
    return found


def is_claude_model(name: str) -> bool:
    return name == PREFIX or name.startswith(PREFIX + ":")


def _default_runner(args: list[str], *, input: str, cwd: str, timeout: int, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=input, cwd=cwd, env=env, capture_output=True, timeout=timeout,
                          encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class ClaudeCodeModel:
    """`Model` backed by `claude -p`. `name` is 'claude-code' (the user's Claude Code default) or 'claude-code:<alias>'."""

    def __init__(self, name: str = PREFIX, cli: Optional[str] = None, runner: Runner = _default_runner):
        if not is_claude_model(name):
            raise AIError(f"Claude Code 모델 이름이 아닙니다: {name}", "config")
        self.name = name
        self.alias = name.partition(":")[2] or None
        self.cli = cli if cli is not None else find_cli()
        self.runner = runner
        if not self.cli:
            raise AIError("Claude Code가 설치되어 있지 않습니다. Claude Code를 설치하고 로그인한 뒤 다시 시도하세요.",
                          "config")
        if is_shim(self.cli):
            raise AIError("Claude Code 실행 파일(claude.exe)을 찾을 수 없습니다. Claude Code를 다시 설치해 보세요.",
                          "config")

    # ------------------------------------------------------------ process

    def _exec(self, args: list[str], stdin: str = "", timeout: int = TIMEOUT_S) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        with tempfile.TemporaryDirectory(prefix="workhistory-claude-") as cwd:  # nothing to read, no CLAUDE.md
            try:
                return self.runner([self.cli, *args], input=stdin, cwd=cwd, timeout=timeout, env=env)
            except subprocess.TimeoutExpired:
                raise AIError("Claude Code가 제시간에 답하지 않았습니다. 기간을 줄이거나 잠시 후 다시 시도하세요.",
                              "server") from None
            except OSError as e:
                raise AIError(f"Claude Code를 실행할 수 없습니다 ({type(e).__name__}).", "config") from None

    def _run(self, system: str, prompt: str, extra: list[str], thinking: Optional[str] = None) -> tuple[dict, Usage]:
        args = ["-p", "--output-format", "json", "--no-session-persistence",
                "--setting-sources", "project",       # the empty cwd has none: no user hooks or permissions
                "--disable-slash-commands", "--max-budget-usd", MAX_BUDGET_USD,
                "--system-prompt", system, "--tools", "", "--strict-mcp-config"]
        if self.alias:
            args += ["--model", self.alias]
        if thinking:
            args += ["--effort", thinking]
        out = self._exec(args + extra, prompt)
        try:
            result = json.loads(out.stdout)
        except json.JSONDecodeError:
            raise _failure(out.stderr or out.stdout) from None
        if not isinstance(result, dict):
            raise _failure(out.stdout)
        usage = _usage(result)
        if result.get("is_error") or out.returncode != 0:
            raise _failure(str(result.get("result") or out.stderr or ""), result.get("api_error_status"),
                           result.get("subtype"))
        return result, usage

    # ------------------------------------------------------------ Model

    def check(self) -> None:
        """Installed and signed in? Sends no content."""
        out = self._exec(["auth", "status"], timeout=60)
        try:
            status = json.loads(out.stdout)
        except json.JSONDecodeError:
            raise AIError("Claude Code 로그인 상태를 확인할 수 없습니다.", "config") from None
        if not status.get("loggedIn"):
            raise AIError("Claude Code에 로그인되어 있지 않습니다. 터미널에서 claude를 한 번 실행해 로그인하세요.", "auth")

    def generate_json(self, system: str, prompt: str, schema: dict,
                      max_output_tokens: int = 16000, thinking: Optional[str] = None) -> tuple[Any, Usage]:
        result, usage = self._run(system, prompt, ["--json-schema", json.dumps(schema, ensure_ascii=False)], thinking)
        data = result.get("structured_output")
        if data is None:
            try:
                data = json.loads(result.get("result") or "")
            except json.JSONDecodeError:
                raise AIError("Claude Code 응답 형식이 올바르지 않습니다. 다시 시도하세요.", "invalid_output") from None
        return data, usage

    def run_mcp(self, system: str, prompt: str, server: list[str], tools: list[str]) -> tuple[str, Usage]:
        """Let Claude Code run the tool loop against one MCP server (`server` = command line).
        Only the named tools of that server are allowed; everything else is off."""
        config = {"mcpServers": {"workhistory": {"type": "stdio", "command": server[0], "args": server[1:]}}}
        result, usage = self._run(system, prompt, [
            "--mcp-config", json.dumps(config, ensure_ascii=False),
            "--allowedTools", ",".join(f"mcp__workhistory__{t}" for t in tools)])
        return str(result.get("result") or "").strip(), usage

    def run_tools(self, *args, **kwargs):
        raise AIError("Claude Code는 앱의 MCP 서버로 도구를 씁니다 (run_mcp).", "config")


def _usage(result: dict) -> Usage:
    u = result.get("usage") or {}
    thinking = (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
    return Usage(
        input_tokens=(u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
        + (u.get("cache_creation_input_tokens") or 0),
        output_tokens=max((u.get("output_tokens") or 0) - thinking, 0),
        thinking_tokens=thinking,
        requests=result.get("num_turns") or 1,
    )


def _failure(message: str, status: Any = None, subtype: Any = None) -> AIError:
    text = (message or "").strip()
    low = text.lower()
    if status in (401, 403) or "not logged in" in low or "/login" in low or "authentication" in low:
        return AIError("Claude Code에 로그인되어 있지 않거나 인증이 만료되었습니다. 터미널에서 claude를 실행해 로그인하세요.",
                       "auth")
    if status == 429 or "usage limit" in low or "rate limit" in low or "limit reached" in low:
        return AIError("Claude 사용 한도에 도달했습니다. 잠시 후 다시 시도하세요.", "quota")
    if subtype == "error_max_budget_usd":
        return AIError("한 번에 쓸 수 있는 AI 사용량을 넘었습니다. 기간을 줄여 다시 시도하세요.", "quota")
    if (isinstance(status, int) and status >= 500) or "overloaded" in low:
        return AIError("Claude 서버에 일시적인 문제가 있습니다. 잠시 후 다시 시도하세요.", "server")
    return AIError("Claude Code 실행이 실패했습니다" + (f": {text[:200]}" if text else "."), "error")

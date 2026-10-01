"""Register / unregister this app's MCP server with Claude Code, from inside the app.

Uses Claude Code's own CLI (`claude mcp add|remove|get`), never edits its
configuration files. Only ever touches the server name this app owns, and
only when that registration points at this app's MCP server module.
"""

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

SERVER_NAME = "workhistory"
DEMO_SERVER_NAME = "workhistory-demo"
MODULE = "workhistory.mcp_server"
TIMEOUT_S = 90
# A Windows .cmd shim runs through cmd.exe: refuse arguments cmd could misread.
_UNSAFE_FOR_CMD = re.compile(r'[&|<>^%!"\r\n]')

Runner = Callable[[list[str], int], subprocess.CompletedProcess]


class ClaudeLinkError(Exception):
    pass


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


def console_python(executable: str = sys.executable) -> str:
    """The interpreter Claude Code should start: pythonw.exe (no console streams) -> python.exe."""
    path = Path(executable)
    if path.stem.lower() == "pythonw":
        sibling = path.with_name("python" + path.suffix)
        if sibling.exists():
            return str(sibling)
    return executable


def _default_runner(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@dataclass
class ClaudeLink:
    demo: bool = False
    cli: Optional[str] = None          # path to the `claude` executable; found on PATH when None
    runner: Runner = _default_runner
    python: str = field(default_factory=console_python)

    def __post_init__(self):
        if self.cli is None:
            self.cli = find_cli()

    @property
    def name(self) -> str:
        return DEMO_SERVER_NAME if self.demo else SERVER_NAME

    def server_command(self) -> list[str]:
        return [self.python, "-m", MODULE] + (["--demo"] if self.demo else [])

    def _claude(self, *args: str) -> subprocess.CompletedProcess:
        if not self.cli:
            raise ClaudeLinkError("Claude Code가 설치되어 있지 않습니다 (claude 명령을 찾을 수 없음).")
        if is_shim(self.cli) and any(_UNSAFE_FOR_CMD.search(a) for a in args):
            raise ClaudeLinkError("경로에 특수문자(& % ! 등)가 있어 자동 등록할 수 없습니다. 수동 설정을 이용하세요.")
        try:
            return self.runner([self.cli, *args], TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ClaudeLinkError("Claude Code가 응답하지 않습니다. 잠시 후 다시 시도하세요.") from None
        except OSError as e:
            raise ClaudeLinkError(f"Claude Code를 실행할 수 없습니다 ({type(e).__name__}).") from None

    # ------------------------------------------------------------ queries

    def version(self) -> Optional[str]:
        if not self.cli:
            return None
        try:
            out = self._claude("--version")
        except ClaudeLinkError:
            return None
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None

    def registration(self) -> Optional[str]:
        """Claude Code's description of our server name, or None if it is not registered."""
        out = self._claude("mcp", "get", self.name)
        if out.returncode != 0:
            return None
        return out.stdout

    def status(self) -> dict:
        if not self.cli:
            return {"installed": False, "name": self.name, "registered": False, "ours": False}
        info = self.registration()
        return {"installed": True, "version": self.version(), "name": self.name,
                "registered": info is not None, "ours": info is not None and MODULE in info}

    # ------------------------------------------------------------ changes

    def register(self) -> dict:
        info = self.registration()
        if info is not None:
            if MODULE not in info:
                raise ClaudeLinkError(f"Claude Code에 '{self.name}'라는 다른 서버가 이미 있어 바꾸지 않았습니다.")
            self._remove()  # re-register so the path is current (e.g. the app moved)
        out = self._claude("mcp", "add", "--scope", "user", "--transport", "stdio", self.name, "--",
                           *self.server_command())
        if out.returncode != 0:
            raise ClaudeLinkError("Claude Code에 등록하지 못했습니다: " + (out.stderr or out.stdout).strip()[:200])
        return self.status()

    def unregister(self) -> dict:
        info = self.registration()
        if info is not None and MODULE in info:
            self._remove()
        return self.status()

    def _remove(self) -> None:
        out = self._claude("mcp", "remove", "--scope", "user", self.name)
        if out.returncode != 0:
            raise ClaudeLinkError("Claude Code 연결을 해제하지 못했습니다: " + (out.stderr or out.stdout).strip()[:200])

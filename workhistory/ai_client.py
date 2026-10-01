"""The provider-neutral interface the AI features are written against.

Today's implementation is Claude Code (`ai_claude.ClaudeCodeModel`). Another
model (e.g. an LLM inside a closed network) plugs in by implementing `Model`:
`generate_json` for reports, and either `run_tools` (the app runs the tool
loop in-process) or `run_mcp` (the model's own agent calls the app's MCP
server) for questions.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

MAX_TOOL_CALLS = 12


class AIError(Exception):
    """A failure the user can act on. `kind` drives retry hints in the UI."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.message = message
        self.kind = kind  # config | not_allowed | auth | quota | network | server | blocked | invalid_output | error


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    requests: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.thinking_tokens += other.thinking_tokens
        self.requests += other.requests


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema (object)


@dataclass
class ToolTrace:
    name: str
    args: dict
    ok: bool


@dataclass
class AgentResult:
    text: str
    usage: Usage
    calls: list[ToolTrace] = field(default_factory=list)


class Model(Protocol):
    name: str

    def check(self) -> None: ...

    def generate_json(self, system: str, prompt: str, schema: dict,
                      max_output_tokens: int = 16000, thinking: Optional[str] = None) -> tuple[Any, Usage]: ...

    def run_tools(self, system: str, prompt: str, tools: list[ToolSpec],
                  execute: Callable[[str, dict], Any], max_calls: int = MAX_TOOL_CALLS) -> AgentResult: ...


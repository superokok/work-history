"""The OpenAI-compatible chat API as a `Model`: what the local model and registered AI servers share.

Almost every LLM server speaks it (llama.cpp, vLLM, Ollama, LM Studio, TGI,
company gateways), so any model behind such a server can write reports and
answer questions. Subclasses only say how to send a request (`_post`).

Servers differ in what they accept, so reports degrade step by step: a JSON
schema (`response_format: json_schema`), else JSON mode (`json_object`), else
plain text with the schema in the prompt. Thinking is switched off where the
server allows it (`chat_template_kwargs`; dropped for servers that reject it):
Qwen3-style models otherwise think for thousands of tokens, which takes many
minutes on a CPU and can cut the answer off. Leftover thinking
(`<think>…</think>`) is removed before the answer is read.
"""

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from .ai_client import MAX_TOOL_CALLS, AIError, AgentResult, ToolSpec, ToolTrace, Usage

MAX_ANSWER_TOKENS = 8192
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")

# No system proxy: the records go to the server the user chose, never through a proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HTTPFailure(AIError):
    """The server answered with an HTTP error (status kept, so callers can fall back)."""

    def __init__(self, status: int, detail: str, message: str):
        super().__init__(message, "server")
        self.status = status
        self.detail = detail


def http_json(url: str, body: Optional[dict] = None, key: str = "", timeout: float = 60) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET", headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        if e.code in (401, 403):
            raise HTTPFailure(e.code, detail, "AI 서버가 접근을 거부했습니다. API 키를 확인하세요.") from None
        raise HTTPFailure(e.code, detail, f"AI 서버가 요청을 처리하지 못했습니다 (HTTP {e.code}).") from None
    except json.JSONDecodeError:
        raise AIError("AI 서버의 응답이 JSON이 아닙니다. 주소가 OpenAI 호환 API인지 확인하세요.", "invalid_output") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise AIError("AI 서버에 연결하지 못했거나 제시간에 답하지 않았습니다. 주소와 네트워크를 확인하세요.",
                      "network") from None


def _clean(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def _too_long(e: HTTPFailure) -> bool:
    low = e.detail.lower()
    return "context" in low and ("exceed" in low or "too long" in low or "maximum" in low)


class OpenAICompatModel:
    """`Model` over `/v1/chat/completions`. Subclasses set `name`, `model_id` and implement `_post`."""

    name = ""
    model_id = ""
    # Sent until the server rejects it: thinking off (llama.cpp, vLLM, SGLang read it; others may refuse).
    no_thinking: Optional[dict] = {"chat_template_kwargs": {"enable_thinking": False}}

    def _post(self, body: dict) -> dict:
        raise NotImplementedError

    def _send(self, body: dict) -> dict:
        if self.no_thinking:
            try:
                return self._post({**body, **self.no_thinking})
            except HTTPFailure as e:
                if e.status not in (400, 422):
                    raise
                self.no_thinking = None  # this server does not take it: ask plainly from now on
        return self._post(body)

    def _chat(self, messages: list[dict], **fields: Any) -> tuple[dict, Usage]:
        body = {"model": self.model_id, "messages": messages, "temperature": 0.2,
                "max_tokens": MAX_ANSWER_TOKENS, **fields}
        try:
            data = self._send(body)
        except HTTPFailure as e:
            if _too_long(e):
                raise AIError("기록이 많아 AI가 한 번에 읽을 수 없습니다. 기간을 줄여 다시 시도하세요.",
                              "invalid_output") from None
            raise
        u = data.get("usage") or {}
        usage = Usage(input_tokens=u.get("prompt_tokens") or 0, output_tokens=u.get("completion_tokens") or 0,
                      requests=1)
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise AIError("AI 응답 형식이 올바르지 않습니다. 다시 시도하세요.", "invalid_output") from None
        if choice.get("finish_reason") == "length":
            raise AIError("AI의 답이 최대 길이에서 잘렸습니다. 기간을 줄이거나, 생각(추론) 모드가 꺼진 모델·서버 설정을 쓰세요.",
                          "invalid_output")
        return message, usage

    def generate_json(self, system: str, prompt: str, schema: dict,
                      max_output_tokens: int = 16000, thinking: Optional[str] = None) -> tuple[Any, Usage]:
        schema_text = json.dumps(schema, ensure_ascii=False)
        attempts = [
            (system, {"response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}}),
            (f"{system}\n\n이 JSON 스키마를 따르는 JSON 객체 하나로만 답하세요:\n{schema_text}",
             {"response_format": {"type": "json_object"}}),
            (f"{system}\n\n이 JSON 스키마를 따르는 JSON 객체 하나로만 답하세요. 다른 글은 쓰지 마세요:\n{schema_text}", {}),
        ]
        for i, (sys_text, fmt) in enumerate(attempts):
            try:
                message, usage = self._chat([{"role": "system", "content": sys_text},
                                             {"role": "user", "content": prompt}],
                                            max_tokens=min(max_output_tokens, MAX_ANSWER_TOKENS), **fmt)
            except HTTPFailure as e:
                if e.status in (400, 404, 415, 422) and i < len(attempts) - 1:
                    continue  # this server does not take that response_format: try the next way
                raise
            text = _FENCE.sub("", _clean(message.get("content") or ""))
            try:
                return json.loads(text), usage
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start >= 0 and end > start:
                    try:
                        return json.loads(text[start:end + 1]), usage
                    except json.JSONDecodeError:
                        pass
                raise AIError("AI 응답 형식이 올바르지 않습니다. 다시 시도하세요.", "invalid_output") from None
        raise AIError("AI 응답 형식이 올바르지 않습니다. 다시 시도하세요.", "invalid_output")

    def run_tools(self, system: str, prompt: str, tools: list[ToolSpec],
                  execute: Callable[[str, dict], Any], max_calls: int = MAX_TOOL_CALLS) -> AgentResult:
        specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": t.parameters}} for t in tools]
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        total, calls = Usage(), []
        while True:
            budget_left = len(calls) < max_calls
            try:
                message, usage = self._chat(messages, **({"tools": specs} if budget_left else {}))
            except HTTPFailure as e:
                if budget_left and not calls and e.status in (400, 422) and "tool" in e.detail.lower():
                    raise AIError("이 AI 서버는 도구 호출(tools)을 지원하지 않아 'AI에게 묻기'에 쓸 수 없습니다. "
                                  "보고서는 만들 수 있습니다.", "config") from None
                raise
            total.add(usage)
            requested = message.get("tool_calls") or []
            if not requested or not budget_left:
                return AgentResult(_clean(message.get("content") or ""), total, calls)
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": requested})
            for call in requested:
                fn = call.get("function") or {}
                name, args = fn.get("name", ""), {}
                try:
                    parsed = fn.get("arguments") or "{}"
                    parsed = json.loads(parsed) if isinstance(parsed, str) else parsed
                    if not isinstance(parsed, dict):
                        raise ValueError("arguments must be an object")
                    args = parsed
                    result, ok = execute(name, args), True
                except Exception as e:  # a bad call is reported back to the model, as the MCP server does
                    result, ok = {"error": str(e)[:300]}, False
                calls.append(ToolTrace(name, args, ok))
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps(result, ensure_ascii=False)})

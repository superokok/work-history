"""AI servers the user registered by address (e.g. an LLM inside the company network).

Any server with an OpenAI-compatible API works (vLLM, Ollama, llama.cpp,
LM Studio, TGI, a company gateway). Each registration is one model choice,
named "remote:<id>". Choosing it is the consent to send the records (redacted,
as for every AI) to that server; the UI says which address they go to.

An API key, if the server needs one, is kept in the app's database on this PC
(the user's own data folder) and never sent back to the UI.
"""

import sqlite3
import time
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from .ai_client import AIError
from .ai_openai import OpenAICompatModel, http_json
from .store import Invalid, NotFound

PREFIX = "remote"
REQUEST_TIMEOUT_S = 1800  # a busy shared server can need minutes for a weekly report
CHECK_TIMEOUT_S = 15


def is_remote_model(name: str) -> bool:
    return name.startswith(PREFIX + ":")


def model_name(endpoint_id: int) -> str:
    return f"{PREFIX}:{endpoint_id}"


def normalize_url(raw: Any) -> str:
    """'10.1.2.3:8000', 'http://llm.corp/v1/', 'https://gw/openai' -> the API base ending in /v1."""
    text = str(raw or "").strip()
    if not text:
        raise Invalid("서버 주소를 입력하세요 (예: http://10.1.2.3:8000)")
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Invalid("서버 주소는 http:// 또는 https:// 로 시작하는 주소여야 합니다")
    if parts.username or parts.password:
        raise Invalid("주소에 계정 정보를 넣지 마세요. API 키 칸을 쓰세요")
    if parts.query or parts.fragment:
        raise Invalid("주소에 ?나 #은 넣을 수 없습니다")
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _text(data: dict, key: str, label: str, limit: int, required: bool = True) -> str:
    value = data.get(key)
    value = "" if value is None else str(value).strip()
    if required and not value:
        raise Invalid(f"{label}을(를) 입력하세요")
    if len(value) > limit:
        raise Invalid(f"{label}이(가) 너무 깁니다")
    return value


def _public(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "model": model_name(row["id"]), "name": row["name"], "url": row["url"],
            "model_id": row["model_id"], "has_key": bool(row["api_key"]), "created_at": row["created_at"]}


def list_endpoints(conn: sqlite3.Connection) -> list[dict]:
    return [_public(r) for r in conn.execute("SELECT * FROM ai_endpoints ORDER BY id")]


def available_models(conn: sqlite3.Connection) -> list[str]:
    return [model_name(r["id"]) for r in conn.execute("SELECT id FROM ai_endpoints ORDER BY id")]


def _row(conn: sqlite3.Connection, endpoint_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,)).fetchone()
    if row is None:
        raise NotFound("등록된 AI 서버를 찾을 수 없습니다")
    return row


def add_endpoint(conn: sqlite3.Connection, data: dict) -> dict:
    name = _text(data, "name", "이름", 60)
    url = normalize_url(data.get("url"))
    model_id = _text(data, "model_id", "모델 이름", 200)
    api_key = _text(data, "api_key", "API 키", 1000, required=False)
    cur = conn.execute("INSERT INTO ai_endpoints (name, url, model_id, api_key, created_at) VALUES (?, ?, ?, ?, ?)",
                       (name, url, model_id, api_key, time.time()))
    return _public(_row(conn, cur.lastrowid))


def update_endpoint(conn: sqlite3.Connection, endpoint_id: int, data: dict) -> dict:
    row = _row(conn, endpoint_id)
    name = _text(data, "name", "이름", 60) if "name" in data else row["name"]
    url = normalize_url(data["url"]) if "url" in data else row["url"]
    model_id = _text(data, "model_id", "모델 이름", 200) if "model_id" in data else row["model_id"]
    # api_key: absent = keep, "" = remove, text = replace (the UI never sees the stored key)
    api_key = _text(data, "api_key", "API 키", 1000, required=False) if "api_key" in data else row["api_key"]
    conn.execute("UPDATE ai_endpoints SET name = ?, url = ?, model_id = ?, api_key = ? WHERE id = ?",
                 (name, url, model_id, api_key, endpoint_id))
    return _public(_row(conn, endpoint_id))


def delete_endpoint(conn: sqlite3.Connection, endpoint_id: int) -> None:
    _row(conn, endpoint_id)
    conn.execute("DELETE FROM ai_endpoints WHERE id = ?", (endpoint_id,))


def fetch_models(url: Any, api_key: Any = "", endpoint_id: Optional[int] = None,
                 conn: Optional[sqlite3.Connection] = None) -> list[str]:
    """The model names a server offers (GET /v1/models). Sends no work data.
    With `endpoint_id`, the stored key is used when no new key is given."""
    key = str(api_key or "").strip()
    if not key and endpoint_id is not None and conn is not None:
        key = _row(conn, endpoint_id)["api_key"]
    data = http_json(normalize_url(url) + "/models", key=key, timeout=CHECK_TIMEOUT_S)
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise AIError("이 주소는 모델 목록(/v1/models)을 주지 않습니다. 모델 이름을 직접 입력하세요.", "invalid_output")
    return sorted({str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")})


def model(conn: sqlite3.Connection, name: str) -> "RemoteModel":
    if not is_remote_model(name):
        raise AIError(f"등록한 AI 서버 모델 이름이 아닙니다: {name}", "config")
    try:
        row = _row(conn, int(name.partition(":")[2]))
    except (ValueError, NotFound):
        raise AIError("등록된 AI 서버를 찾을 수 없습니다. 개인정보 탭의 AI 설정을 확인하세요.", "config") from None
    return RemoteModel(row["id"], row["name"], row["url"], row["model_id"], row["api_key"])


class RemoteModel(OpenAICompatModel):
    """`Model` backed by a registered OpenAI-compatible server."""

    def __init__(self, endpoint_id: int, label: str, url: str, model_id: str, api_key: str = ""):
        self.name = model_name(endpoint_id)
        self.label = label
        self.url = url
        self.model_id = model_id
        self.api_key = api_key

    def _post(self, body: dict) -> dict:
        return http_json(self.url + "/chat/completions", body, self.api_key, REQUEST_TIMEOUT_S)

    def check(self) -> None:
        """Reachable, key accepted, model offered. Sends no work data."""
        models = fetch_models(self.url, self.api_key)
        if models and self.model_id not in models:
            raise AIError(f"서버에 '{self.model_id}' 모델이 없습니다. 있는 모델: {', '.join(models[:8])}", "config")

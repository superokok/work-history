"""Minimal client for ActivityWatch's local REST API (api/0)."""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional


class AWError(Exception):
    """ActivityWatch is unreachable or returned something unexpected."""


def to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def parse_iso(value: str) -> float:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:  # AW always sends UTC; be defensive anyway
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class AWClient:
    def __init__(self, base_url: str, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Never route local activity data through a system/env proxy (and skip
        # the per-request Windows registry proxy lookup).
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _get(self, path: str, params: Optional[dict[str, str]] = None) -> Any:
        url = f"{self.base_url}/api/0/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with self._opener.open(url, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise AWError(f"ActivityWatch HTTP {e.code} for {path.split('?')[0]}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise AWError(f"ActivityWatch unreachable at {self.base_url}") from e
        except json.JSONDecodeError as e:
            raise AWError("ActivityWatch returned invalid JSON") from e

    def info(self) -> dict:
        return self._get("info")

    def buckets(self) -> dict[str, dict]:
        return self._get("buckets/")

    def events(self, bucket_id: str, start: float, end: float) -> list[dict]:
        """Events overlapping [start, end]; AW trims them to the range."""
        path = f"buckets/{urllib.parse.quote(bucket_id, safe='')}/events"
        result = self._get(path, {"start": to_iso(start), "end": to_iso(end)})
        if not isinstance(result, list):
            raise AWError("Unexpected events payload from ActivityWatch")
        return result

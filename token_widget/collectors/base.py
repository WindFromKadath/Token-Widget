"""采集器基础：共享 HTTP 原语、错误类型与映射工具函数（DESIGN.md §4）。"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from datetime import datetime


class CollectorError(Exception):
    """采集失败；code 取值见 models.ErrorCode。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def raw_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: int = 10,
) -> tuple[int, dict, bytes]:
    """发起一次 HTTP 请求，不自动跟随重定向；3xx/4xx/5xx 均原样返回。

    返回 (status, headers, body)；headers 键统一小写（HTTP 头大小写不敏感，
    Node 系服务器常发小写 location）。网络层异常（超时/连接失败/截断）抛
    CollectorError('fetch_failed')。
    """
    req = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, _lower_headers(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        # 30x（禁止自动重定向）与非 2xx 都落到这里
        return exc.code, _lower_headers(exc.headers or {}), exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise CollectorError("fetch_failed", str(exc)) from exc


def _lower_headers(headers) -> dict:
    """响应头名统一小写，消费方按小写键读取。"""
    return {str(k).lower(): v for k, v in dict(headers).items()}


# ---------------------------------------------------------------- 映射工具（§4 各采集器共享）

def parse_num(value) -> float | None:
    """宽松数值解析：bool -> None；int/float/数字字符串 -> float；其他 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def clamp_percent(value: float) -> float:
    """已用百分比钳制到 0-100（§2 约定）。"""
    return min(max(value, 0.0), 100.0)


def reset_to_seconds(value, now: float) -> int | None:
    """绝对重置时间 -> 距重置秒数；兼容 ISO 字符串 / 秒 / 毫秒时间戳（阈值 1e12）。"""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(int(dt.timestamp() - now), 0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0:
            return None
        ms = value * 1000 if value < 1e12 else value
        return max(int(ms / 1000 - now), 0)
    return None


def parse_json_body(body: bytes) -> dict:
    """响应体 -> dict；非 JSON 或非对象抛 CollectorError('parse_failed')。"""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise CollectorError("parse_failed", str(exc)) from exc
    if not isinstance(payload, dict):
        raise CollectorError("parse_failed", "响应不是 JSON 对象")
    return payload


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),                 # OpenAI/Anthropic 等 sk- 密钥
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),  # Authorization Bearer 值
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),                # Google API Key
)


def redact_secrets(text: str) -> str:
    """已知密钥样式（sk-*、Bearer 值、Google Key）脱敏为 ***（§9：展示/日志不落密钥）。"""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***", text)
    return text

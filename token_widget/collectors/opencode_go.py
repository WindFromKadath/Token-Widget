"""OpenCode Go 采集器（DESIGN.md §4.4，原生实现）。

自 ../legacy/opencode-go-bridge/bridge.py 移植（解析逻辑已在该处经 16 个测试与
真实账号验证）：挂件内化此逻辑，不再依赖独立桥接服务。
"""

from __future__ import annotations

import re
import time
import urllib.parse

from ..config import OpencodeGoAccount
from ..models import QuotaResult, QuotaWindow
from .base import CollectorError, DEFAULT_USER_AGENT, clamp_percent, raw_request

LOGIN_PAGE_MARKER = "<title>OpenAuth</title>"
MAX_REDIRECTS = 5

_WINDOW_META = {  # 解析键 -> (QuotaWindow.key, label)
    "rolling": ("5h", "5小时"),
    "weekly": ("weekly", "每周"),
    "monthly": ("monthly", "每月"),
}


def _is_opencode_host(hostname: str | None) -> bool:
    """opencode.ai 或其子域（auth.opencode.ai 等）。"""
    return hostname == "opencode.ai" or bool(hostname) and hostname.endswith(".opencode.ai")


def _request_headers(url: str, cookie: str) -> dict:
    """按目标域构造请求头：仅 opencode.ai 及子域附带 auth Cookie（§9）。"""
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html",
    }
    if _is_opencode_host(urllib.parse.urlparse(url).hostname):
        headers["Cookie"] = cookie
    return headers


# ---------------------------------------------------------------- 抓取

def fetch_html(
    account: OpencodeGoAccount,
    *,
    timeout: int = 10,
    request=raw_request,
) -> str:
    """手动跟随重定向抓取工作区页面；识别登录页 -> cookie_expired。"""
    url = f"https://opencode.ai/workspace/{account.workspace_id}/go"
    cookie = f"auth={account.auth_cookie}; oc_locale=zh"
    headers = _request_headers(url, cookie)
    for _ in range(MAX_REDIRECTS):
        status, resp_headers, body = request(url, headers=headers, timeout=timeout)
        location = resp_headers.get("location")  # raw_request 响应头名统一小写
        if status in (301, 302, 303, 307, 308) and location:
            url = urllib.parse.urljoin(url, location)
            headers = _request_headers(url, cookie)  # 跨域跳转去 Cookie
            continue
        html = body.decode("utf-8", errors="replace")
        final = urllib.parse.urlparse(url)
        if (
            final.hostname == "auth.opencode.ai"
            or (final.hostname == "opencode.ai" and final.path.startswith("/auth"))
            or LOGIN_PAGE_MARKER in html
        ):
            raise CollectorError("cookie_expired")
        if status != 200:
            raise CollectorError("fetch_failed", f"上游返回 HTTP {status}")
        return html
    raise CollectorError("fetch_failed", "重定向次数过多")


# ---------------------------------------------------------------- 解析
# 内联 hydration 数据形如：
#   rollingUsage: $R[31] = { status: "ok", resetInSec: 18000, usagePercent: 0 }
# 值块非严格 JSON（可能含 !0、new Date() 等），逐字段正则抽取。

_USAGE_KEYS = {"rolling": "rollingUsage", "weekly": "weeklyUsage", "monthly": "monthlyUsage"}
_USAGE_VALUE_RE = r"\s*:\s*(?:\$R\[\d+\]\s*=\s*)?\{"


def _extract_usage_block(html: str, key: str) -> str | None:
    """截出 `<key>:` 之后的 {...} 块（跳过 `:null` 这类非用量重复出现）。"""
    m = re.search(re.escape(key) + _USAGE_VALUE_RE, html)
    if not m:
        return None
    brace_start = m.end() - 1
    depth = 0
    for i in range(brace_start, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[brace_start : i + 1]
    return None


def _parse_int_field(block: str, field: str) -> int | None:
    m = re.search(re.escape(field) + r"\s*:\s*(-?\d+)", block)
    return int(m.group(1)) if m else None


def _parse_str_field(block: str, field: str) -> str | None:
    m = re.search(re.escape(field) + r'\s*:\s*"([^"]*)"', block)
    return m.group(1) if m else None


def parse_inline(html: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for name, key in _USAGE_KEYS.items():
        block = _extract_usage_block(html, key)
        if not block:
            continue
        percent = _parse_int_field(block, "usagePercent")
        if percent is None:
            continue
        result[name] = {
            "percent": percent,
            "reset_in_sec": _parse_int_field(block, "resetInSec"),
            "status": _parse_str_field(block, "status"),
            "reset_text": None,
        }
    return result


_DOM_ORDER = ("rolling", "weekly", "monthly")
_USAGE_ITEM_RE = re.compile(r'data-slot="usage-item"')


def _clean_reset_text(raw: str) -> str | None:
    cleaned = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def parse_dom(html: str) -> dict[str, dict]:
    """DOM 兜底：3 个 data-slot="usage-item" 块，顺序 rolling → weekly → monthly。"""
    result: dict[str, dict] = {}
    starts = [m.start() for m in _USAGE_ITEM_RE.finditer(html)]
    for idx, name in enumerate(_DOM_ORDER):
        if idx >= len(starts):
            break
        seg_start = starts[idx]
        seg_end = starts[idx + 1] if idx + 1 < len(starts) else seg_start + 800
        segment = html[seg_start:seg_end]
        m = re.search(
            r'data-slot="usage-value">\s*(?:<!--.*?-->)?\s*(\d+)', segment, re.S
        ) or re.search(r"width:\s*(\d+)%", segment)
        if not m:
            continue
        rt = re.search(r'data-slot="reset-time">\s*(.*?)</span>', segment, re.S)
        result[name] = {
            "percent": int(m.group(1)),
            "reset_in_sec": None,
            "status": None,
            "reset_text": _clean_reset_text(rt.group(1)) if rt else None,
        }
    return result


def parse_usage(html: str) -> dict[str, dict]:
    """内联优先，缺失项用 DOM 补齐。"""
    inline = parse_inline(html)
    if len(inline) == 3:
        return inline
    dom = parse_dom(html)
    merged = dict(dom)
    merged.update(inline)
    return merged


def is_no_subscription(html: str) -> bool:
    """无 Go 订阅页：存在订阅按钮且 billing 块 lite / liteSubscriptionID 为 null。"""
    if 'data-slot="subscribe-button"' not in html:
        return False
    return bool(re.search(r"lite\s*:\s*null", html)) and bool(
        re.search(r"liteSubscriptionID\s*:\s*null", html)
    )


# ---------------------------------------------------------------- 采集

def collect(
    plan_id: str,
    account: OpencodeGoAccount,
    *,
    timeout: int = 10,
    request=raw_request,
) -> QuotaResult:
    """抓取并解析，输出 3 个 QuotaWindow（5h/weekly/monthly）。

    网络失败由调用方（调度器）回退上次成功数据并置 stale；此处直接抛
    CollectorError 让错误码保持单一来源。
    """
    html = fetch_html(account, timeout=timeout, request=request)
    if is_no_subscription(html):
        raise CollectorError("no_subscription")
    usages = parse_usage(html)
    if not usages:
        raise CollectorError("parse_failed", "页面结构变化，未解析出用量")

    windows: list[QuotaWindow] = []
    for name in ("rolling", "weekly", "monthly"):
        u = usages.get(name)
        if not u:
            continue
        key, label = _WINDOW_META[name]
        windows.append(
            QuotaWindow(
                key=key,
                label=label,
                used_percent=clamp_percent(float(u["percent"])),  # §2：统一钳 0-100
                reset_in_sec=u["reset_in_sec"],
                used_text=u["reset_text"],  # DOM 兜底时只有重置原文
            )
        )
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        plan_label="OpenCode Go",
        fetched_at=int(time.time()),
    )

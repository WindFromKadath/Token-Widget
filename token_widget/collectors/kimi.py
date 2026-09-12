"""Kimi For Coding 采集器（DESIGN.md §4.3.1，M1 必做）。

响应映射核实自 CC Switch `src-tauri/src/services/coding_plan.rs::query_kimi`：
- limits[].detail.{limit, remaining, resetTime} -> 5 小时窗口
- usage.{limit, remaining, resetTime}           -> 周限额窗口
- utilization = max(limit - remaining, 0) / limit * 100
- resetTime 兼容 ISO 8601 字符串 / 秒级 / 毫秒级数字（阈值 1e12）
"""

from __future__ import annotations

import json
import time

from ..credentials import Credentials
from ..models import QuotaResult, QuotaWindow
from .base import (
    CollectorError,
    clamp_percent,
    parse_num,
    raw_request,
    reset_to_seconds,
)

USAGE_URL = "https://api.kimi.com/coding/v1/usages"


def collect(
    plan_id: str,
    creds: Credentials,
    *,
    timeout: int = 15,
    request=raw_request,
) -> QuotaResult:
    if not creds.api_key:
        raise CollectorError("auth_failed", "缺少 API Key")
    status, _headers, body = request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {creds.api_key}",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    if status in (401, 403):
        raise CollectorError("auth_failed")
    if status < 200 or status >= 300:
        raise CollectorError("fetch_failed", f"上游返回 HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise CollectorError("parse_failed", str(exc)) from exc

    windows = map_kimi(payload, now=time.time())
    if not windows:
        raise CollectorError("parse_failed", "响应中无 limits/usage 窗口")
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        plan_label="Kimi For Coding",
        fetched_at=int(time.time()),
    )


# ---------------------------------------------------------------- 映射

def map_kimi(payload: dict, now: float) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    if not isinstance(payload, dict):
        return windows

    limits = payload.get("limits")
    if isinstance(limits, list):
        for item in limits:
            detail = (item or {}).get("detail") if isinstance(item, dict) else None
            window = _tier_to_window(detail, key="5h", label="5小时", now=now)
            if window:
                windows.append(window)

    window = _tier_to_window(payload.get("usage"), key="weekly", label="每周", now=now)
    if window:
        windows.append(window)
    return windows


def _tier_to_window(detail, *, key: str, label: str, now: float) -> QuotaWindow | None:
    if not isinstance(detail, dict):
        return None
    limit = parse_num(detail.get("limit"))
    remaining = parse_num(detail.get("remaining"))
    if limit is None or remaining is None:
        return None
    used = max(limit - remaining, 0.0)
    percent = (used / limit * 100.0) if limit > 0 else 0.0
    return QuotaWindow(
        key=key,
        label=label,
        used_percent=clamp_percent(percent),
        reset_in_sec=reset_to_seconds(detail.get("resetTime"), now),
    )

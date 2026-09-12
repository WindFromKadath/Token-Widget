"""原生余额采集器（templateType == "balance"，DESIGN.md §4.7）。

移植自 CC Switch `src-tauri/src/services/balance.rs`（2026-08 线上版）：
按 base_url 子串识别供应商，各供应商走各自余额端点；输出为"余额类"窗口
（used_percent=None，used_text 展示金额，无重置倒计时）。

支持：DeepSeek / StepFun / SiliconFlow（CN/EN）/ OpenRouter / Novita AI。
未知供应商返回 unsupported（与 CC Switch "Unknown balance provider" 一致）。
"""

from __future__ import annotations

import time

from ..models import QuotaResult, QuotaWindow
from .base import (
    CollectorError,
    clamp_percent,
    parse_json_body,
    parse_num,
    raw_request,
)

TIMEOUT_DEFAULT = 15


def _get(url: str, api_key: str, timeout: int, request) -> dict:
    status, _resp_headers, body = request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    if status in (401, 403):
        raise CollectorError("auth_failed", f"鉴权失败（HTTP {status}）")
    if status < 200 or status >= 300:
        raise CollectorError("fetch_failed", f"上游返回 HTTP {status}")
    return parse_json_body(body)


def _balance_result(
    plan_id: str,
    entries: list[tuple[str, float | None, str | None]],
    *,
    extra: str | None = None,
) -> QuotaResult:
    """entries: (展示 label, remaining, unit)。used_percent=None 的余额窗口。"""
    windows: list[QuotaWindow] = []
    for label, remaining, unit in entries:
        if remaining is None:
            continue
        windows.append(
            QuotaWindow(
                key="balance",
                label=label,
                used_percent=None,
                used_text=_balance_text(remaining, unit),
            )
        )
    if not windows:
        raise CollectorError("parse_failed", "响应中无余额数据")
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        extra=extra,
        fetched_at=int(time.time()),
    )


def _balance_text(amount: float, unit: str | None) -> str:
    unit = (unit or "").upper()
    if unit == "USD":
        return f"${amount:.2f}"
    if unit == "CNY":
        return f"¥{amount:.2f}"
    if unit:
        return f"{amount:.2f} {unit}"
    return f"{amount:.2f}"


# ---------------------------------------------------------------- 各供应商

def _query_deepseek(plan_id: str, api_key: str, timeout: int, request) -> QuotaResult:
    payload = _get("https://api.deepseek.com/user/balance", api_key, timeout, request)
    is_available = payload.get("is_available")
    entries: list[tuple[str, float | None, str | None]] = []
    infos = payload.get("balance_infos")
    if isinstance(infos, list):
        for info in infos:
            if not isinstance(info, dict):
                continue
            currency = info.get("currency") if isinstance(info.get("currency"), str) else None
            entries.append((
                f"余额 {currency}" if currency else "余额",
                parse_num(info.get("total_balance")),
                currency or "CNY",
            ))
    return _balance_result(
        plan_id,
        entries,
        extra=None if is_available is not False else "余额不足",
    )


def _query_stepfun(plan_id: str, api_key: str, timeout: int, request) -> QuotaResult:
    payload = _get("https://api.stepfun.com/v1/accounts", api_key, timeout, request)
    return _balance_result(plan_id, [("余额", parse_num(payload.get("balance")), "CNY")])


def _query_siliconflow(plan_id: str, api_key: str, timeout: int, request, is_cn: bool) -> QuotaResult:
    domain = "api.siliconflow.cn" if is_cn else "api.siliconflow.com"
    payload = _get(f"https://{domain}/v1/user/info", api_key, timeout, request)
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    unit = "CNY" if is_cn else "USD"
    return _balance_result(plan_id, [("余额", parse_num(data.get("totalBalance")), unit)])


def _query_openrouter(plan_id: str, api_key: str, timeout: int, request) -> QuotaResult:
    payload = _get("https://openrouter.ai/api/v1/credits", api_key, timeout, request)
    data = payload.get("data")
    data = data if isinstance(data, dict) else payload
    total = parse_num(data.get("total_credits"))
    used = parse_num(data.get("total_usage"))
    remaining = None
    used_percent = None
    if total is not None:
        remaining = max(total - (used or 0.0), 0.0)
        used_percent = clamp_percent((used or 0.0) / total * 100.0) if total > 0 else 0.0
    if remaining is None:
        raise CollectorError("parse_failed", "响应中无 total_credits")
    windows = [
        QuotaWindow(
            key="balance",
            label="余额",
            used_percent=used_percent,
            used_text=_balance_text(remaining, "USD"),
        )
    ]
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        extra="余额不足" if remaining <= 0 else None,
        fetched_at=int(time.time()),
    )


def _query_novita(plan_id: str, api_key: str, timeout: int, request) -> QuotaResult:
    payload = _get("https://api.novita.ai/v3/user/balance", api_key, timeout, request)
    available = parse_num(payload.get("availableBalance"))
    if available is None:
        raise CollectorError("parse_failed", "响应中无 availableBalance")
    available_usd = available / 10000.0  # Novita 金额单位 0.0001 USD
    return _balance_result(
        plan_id,
        [("余额", available_usd, "USD")],
        extra=None if available_usd > 0 else "余额不足",
    )


# ---------------------------------------------------------------- 路由

_ROUTES = (
    ("api.deepseek.com", lambda p, k, t, r: _query_deepseek(p, k, t, r)),
    ("api.stepfun.ai", lambda p, k, t, r: _query_stepfun(p, k, t, r)),
    ("api.stepfun.com", lambda p, k, t, r: _query_stepfun(p, k, t, r)),
    ("api.siliconflow.cn", lambda p, k, t, r: _query_siliconflow(p, k, t, r, True)),
    ("api.siliconflow.com", lambda p, k, t, r: _query_siliconflow(p, k, t, r, False)),
    ("openrouter.ai", lambda p, k, t, r: _query_openrouter(p, k, t, r)),
    ("api.novita.ai", lambda p, k, t, r: _query_novita(p, k, t, r)),
)


def collect(
    plan_id: str,
    api_key: str | None,
    base_url: str | None,
    *,
    timeout: int = TIMEOUT_DEFAULT,
    request=raw_request,
) -> QuotaResult:
    """按 base_url 识别供应商并查余额；未识别 -> unsupported。"""
    if not api_key:
        raise CollectorError("auth_failed", "缺少 API Key")
    if base_url:
        lowered = base_url.lower()
        for substr, query in _ROUTES:
            if substr in lowered:
                return query(plan_id, api_key, timeout, request)
    raise CollectorError("unsupported", f"未识别的余额供应商（{base_url or '缺少 base_url'}）")

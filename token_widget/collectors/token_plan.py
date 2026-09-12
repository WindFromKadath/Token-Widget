"""token_plan 内置模板（DESIGN.md §4.3）：zhipu / zhipu_team / minimax / zenmux。

端点、字段映射与鉴权细节核对自 CC Switch
`src-tauri/src/services/coding_plan.rs`（2026-08 线上版），偏离 DESIGN.md 处
以源码为准：
- 智谱 Authorization 头**不加 Bearer 前缀**（源码注释明确；§4.3.2 勘误见 DESIGN.md）
- 智谱团队版走固定国内站 + `?type=2`，组织/项目 ID 请求头来自
  `usage_script.teamOrganizationId` / `teamProjectId`（CC Switch 中配置）
- MiniMax 国内/国际站按 base_url 域名子串判定（minimaxi.com / minimax.io）
- ZenMux 的 base_url 本身就是配额端点；`usage_percentage` 为 0-1 小数，×100
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
    redact_secrets,
    reset_to_seconds,
)

TIMEOUT_DEFAULT = 15

# ---------------------------------------------------------------- 共享工具

def _business_error(message: str) -> CollectorError:
    """上游业务级错误（HTTP 2xx 但业务码失败）统一映射；message 脱敏（§9）。"""
    return CollectorError("upstream_error", redact_secrets(message))


def _http_error(status: int, request_url: str) -> CollectorError:
    if status in (401, 403):
        return CollectorError("auth_failed")
    return CollectorError("fetch_failed", f"上游返回 HTTP {status}（{request_url}）")


# ---------------------------------------------------------------- 智谱 GLM

ZHIPU_CN_BASE = "https://open.bigmodel.cn"
ZHIPU_EN_BASE = "https://api.z.ai"
ZHIPU_QUOTA_PATH = "/api/monitor/usage/quota/limit"


def zhipu_quota_base(base_url: str | None) -> str:
    """§4.3.2：国内/国际站按 base_url 子串判定（与 CC Switch detect_provider 一致）。"""
    if base_url and "bigmodel.cn" in base_url.lower():
        return ZHIPU_CN_BASE
    return ZHIPU_EN_BASE


def collect_zhipu(
    plan_id: str,
    api_key: str | None,
    base_url: str | None,
    *,
    team_org: str | None = None,
    team_project: str | None = None,
    timeout: int = TIMEOUT_DEFAULT,
    request=raw_request,
) -> QuotaResult:
    """个人版/团队版智谱额度；团队版由 team_org/team_project 触发。"""
    if not api_key:
        raise CollectorError("auth_failed", "缺少 API Key")

    if team_org or team_project:
        if not (team_org and team_project):
            raise CollectorError(
                "auth_failed",
                "智谱团队版需组织 ID + 项目 ID（在 CC Switch 用量脚本中填写）",
            )
        url = f"{ZHIPU_CN_BASE}{ZHIPU_QUOTA_PATH}?type=2"
        headers = {
            "Authorization": api_key,  # 智谱不加 Bearer 前缀
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en",
            "bigmodel-organization": team_org,
            "bigmodel-project": team_project,
        }
    else:
        url = f"{zhipu_quota_base(base_url)}{ZHIPU_QUOTA_PATH}"
        headers = {
            "Authorization": api_key,  # 智谱不加 Bearer 前缀
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en",
        }

    status, _resp_headers, body = request(url, headers=headers, timeout=timeout)
    if status < 200 or status >= 300:
        raise _http_error(status, url)
    payload = parse_json_body(body)

    if payload.get("success") is False:
        raise _business_error(str(payload.get("msg") or "智谱业务错误"))
    windows, level = map_zhipu(payload, now=time.time())
    if not windows:
        raise CollectorError("parse_failed", "响应中无 TOKENS_LIMIT 额度条目")
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        plan_label="Zhipu GLM Team" if team_org else "Zhipu GLM",
        extra=level,
        fetched_at=int(time.time()),
    )


def map_zhipu(payload: dict, now: float) -> tuple[list[QuotaWindow], str | None]:
    """data.limits[].type == TOKENS_LIMIT 条目 -> (windows, 套餐等级)。

    窗口分类（核实自 CC Switch parse_zhipu_token_tiers）：
    - unit == 3 -> 5 小时滚动；unit == 6 -> 每周
    - 兜底：未分类条目按（无重置时间优先，重置时间升序）填入空槽
    - percentage 即已用百分比（0-100）；nextResetTime 为毫秒时间戳
    """
    windows: list[QuotaWindow] = []
    if not isinstance(payload, dict):
        return windows, None
    data = payload.get("data")
    if not isinstance(data, dict):
        return windows, None

    level = data.get("level")
    level = str(level) if isinstance(level, str) else None

    five_hour: tuple[float | None, float] | None = None
    weekly: tuple[float | None, float] | None = None
    unclassified: list[tuple[float | None, float]] = []

    limits = data.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").upper() != "TOKENS_LIMIT":
                continue
            reset = parse_num(item.get("nextResetTime"))
            percentage = parse_num(item.get("percentage")) or 0.0
            entry = (reset, percentage)
            unit = parse_num(item.get("unit"))
            if unit == 3 and five_hour is None:
                five_hour = entry
            elif unit == 6 and weekly is None:
                weekly = entry
            else:
                unclassified.append(entry)

    unclassified.sort(key=lambda e: (e[0] is not None, e[0] if e[0] is not None else 0.0))
    for entry in unclassified:
        if five_hour is None:
            five_hour = entry
        elif weekly is None:
            weekly = entry
        else:
            break

    for key, label, entry in (
        ("5h", "5小时", five_hour),
        ("weekly", "每周", weekly),
    ):
        if entry is None:
            continue
        windows.append(
            QuotaWindow(
                key=key,
                label=label,
                used_percent=clamp_percent(entry[1]),
                reset_in_sec=reset_to_seconds(entry[0], now),
            )
        )
    return windows, level


# ---------------------------------------------------------------- MiniMax

MINIMAX_CN_HOST = "https://api.minimaxi.com"
MINIMAX_EN_HOST = "https://api.minimax.io"
MINIMAX_PATH = "/v1/api/openplatform/coding_plan/remains"


def minimax_host(base_url: str | None) -> str:
    """§4.3.3：国内/国际站域名判定（api.minimaxi.com / api.minimax.io）。"""
    if base_url and "minimaxi.com" in base_url.lower():
        return MINIMAX_CN_HOST
    return MINIMAX_EN_HOST


def collect_minimax(
    plan_id: str,
    api_key: str | None,
    base_url: str | None,
    *,
    timeout: int = TIMEOUT_DEFAULT,
    request=raw_request,
) -> QuotaResult:
    if not api_key:
        raise CollectorError("auth_failed", "缺少 API Key")
    url = f"{minimax_host(base_url)}{MINIMAX_PATH}"
    status, _resp_headers, body = request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise _http_error(status, url)
    payload = parse_json_body(body)

    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict) and parse_num(base_resp.get("status_code")) not in (None, 0):
        msg = str(base_resp.get("status_msg") or "MiniMax 业务错误")
        raise _business_error(msg)

    windows = map_minimax(payload, now=time.time())
    if not windows:
        raise CollectorError("parse_failed", "响应中无 general 模型额度")
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        plan_label="MiniMax",
        fetched_at=int(time.time()),
    )


def map_minimax(payload: dict, now: float) -> list[QuotaWindow]:
    """model_remains[] 中 model_name == 'general' 的条目 -> windows。

    核实自 CC Switch parse_minimax_tiers：
    - current_*_remaining_percent 是**剩余**百分比 -> 已用 = 100 - 剩余
    - 周桶仅 current_weekly_status == 1 时激活（无周限额套餐为 3，不展示）
    - end_time / weekly_end_time 为毫秒时间戳
    """
    windows: list[QuotaWindow] = []
    if not isinstance(payload, dict):
        return windows
    remains = payload.get("model_remains")
    if not isinstance(remains, list):
        return windows
    item = next(
        (i for i in remains if isinstance(i, dict) and i.get("model_name") == "general"),
        None,
    )
    if not isinstance(item, dict):
        return windows

    remain_5h = parse_num(item.get("current_interval_remaining_percent"))
    if remain_5h is not None:
        windows.append(
            QuotaWindow(
                key="5h",
                label="5小时",
                used_percent=clamp_percent(100.0 - remain_5h),
                reset_in_sec=reset_to_seconds(item.get("end_time"), now),
            )
        )
    if item.get("current_weekly_status") == 1:
        remain_week = parse_num(item.get("current_weekly_remaining_percent"))
        if remain_week is not None:
            windows.append(
                QuotaWindow(
                    key="weekly",
                    label="每周",
                    used_percent=clamp_percent(100.0 - remain_week),
                    reset_in_sec=reset_to_seconds(item.get("weekly_end_time"), now),
                )
            )
    return windows


# ---------------------------------------------------------------- ZenMux

def collect_zenmux(
    plan_id: str,
    api_key: str | None,
    base_url: str | None,
    *,
    timeout: int = TIMEOUT_DEFAULT,
    request=raw_request,
) -> QuotaResult:
    """§4.3.4：base_url 本身就是配额端点。"""
    if not api_key:
        raise CollectorError("auth_failed", "缺少 API Key")
    if not base_url:
        raise CollectorError("auth_failed", "缺少配额端点（base_url，在 CC Switch 用量脚本中配置）")
    status, _resp_headers, body = request(
        base_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise _http_error(status, base_url)
    payload = parse_json_body(body)

    if payload.get("success") is not True:
        raise _business_error(str(payload.get("message") or "ZenMux 业务错误"))
    windows, info = map_zenmux(payload, now=time.time())
    if not windows:
        raise CollectorError("parse_failed", "响应中无 quota_5_hour / quota_7_day 窗口")
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        plan_label="ZenMux",
        extra=info,
        fetched_at=int(time.time()),
    )


def map_zenmux(payload: dict, now: float) -> tuple[list[QuotaWindow], str | None]:
    """data.quota_5_hour / quota_7_day -> (windows, 套餐信息)。

    核实自 CC Switch query_zenmux：
    - usage_percentage 为 0-1 小数，已用百分比 = ×100
    - resets_at 为 ISO 字符串；used_value_usd / max_value_usd -> used_text
    - data.plan.tier + data.account_status -> 套餐信息
    """
    windows: list[QuotaWindow] = []
    info: str | None = None
    if not isinstance(payload, dict):
        return windows, info
    data = payload.get("data")
    if not isinstance(data, dict):
        return windows, info

    for field, key, label in (
        ("quota_5_hour", "5h", "5小时"),
        ("quota_7_day", "weekly", "每周"),
    ):
        win = data.get(field)
        if not isinstance(win, dict):
            continue
        frac = parse_num(win.get("usage_percentage"))
        if frac is None:
            continue
        used_usd = parse_num(win.get("used_value_usd"))
        max_usd = parse_num(win.get("max_value_usd"))
        used_text = None
        if used_usd is not None and max_usd is not None:
            used_text = f"${used_usd:.2f} / ${max_usd:.2f}"
        windows.append(
            QuotaWindow(
                key=key,
                label=label,
                used_percent=clamp_percent(frac * 100.0),
                reset_in_sec=reset_to_seconds(win.get("resets_at"), now),
                used_text=used_text,
            )
        )

    plan = data.get("plan")
    tier = plan.get("tier") if isinstance(plan, dict) else None
    if isinstance(tier, str) and tier:
        account_status = data.get("account_status")
        info = (
            f"{tier} ({account_status})"
            if isinstance(account_status, str) and account_status
            else tier
        )
    return windows, info

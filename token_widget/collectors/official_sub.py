"""官方订阅采集器（DESIGN.md §4.5，M2）：Claude / Codex / Gemini OAuth 额度。

凭据复用各官方 CLI 的登录文件（与 CC Switch `subscription.rs` 一致）：
- Claude: `~/.claude/.credentials.json`  -> claudeAiOauth.accessToken
- Codex:  `~/.codex/auth.json`           -> tokens.access_token（仅 auth_mode=chatgpt）
- Gemini: `~/.gemini/oauth_creds.json`   -> access_token + refresh_token

只读 CLI 文件、凭据仅内存使用（§9）；不写 CC Switch（P1）。过期语义与 CC Switch
一致：文件标记过期时仍尝试调用 API（token 可能实际有效），API 拒绝才报登录失效。

端点与字段映射（核实自 CC Switch 2026-08 线上版）：
- Claude: GET https://api.anthropic.com/api/oauth/usage
          响应顶层键 five_hour/seven_day/seven_day_opus/seven_day_sonnet（+未知键）
          为窗口 {utilization 0-100, resets_at ISO}；extra_usage 为超额用量
- Codex:  GET https://chatgpt.com/backend-api/wham/usage
          {rate_limit: {primary_window/secondary_window}}，
          used_percent + limit_window_seconds（映射窗口名）+ reset_at（unix 秒）
- Gemini: POST loadCodeAssist -> project id；POST retrieveUserQuota ->
          buckets[]（modelId + remainingFraction 0-1 + resetTime），按模型分类、
          同类取最紧张（remainingFraction 最小）；过期时用 refresh_token 刷新
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from ..models import QuotaResult, QuotaWindow
from .base import (
    CollectorError,
    clamp_percent,
    parse_json_body,
    parse_num,
    raw_request,
    reset_to_seconds,
)

TIMEOUT_DEFAULT = 15

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
GEMINI_LOAD_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
GEMINI_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
GEMINI_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Gemini CLI 公开 OAuth client 凭据（来自 google-gemini/gemini-cli，与 CC Switch 一致）
GEMINI_OAUTH_CLIENT_ID = (
    "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
)
GEMINI_OAUTH_CLIENT_SECRET = "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"

# ---------------------------------------------------------------- 路由

_CLAUDE_TIER_LABELS = {
    "five_hour": "5小时",
    "seven_day": "每周",
    "seven_day_opus": "每周 Opus",
    "seven_day_sonnet": "每周 Sonnet",
}

_GEMINI_TIER_LABELS = {
    "gemini_pro": "Gemini Pro",
    "gemini_flash": "Gemini Flash",
    "gemini_flash_lite": "Flash Lite",
}


def tool_for_app_type(app_type: str) -> str | None:
    """app_type -> 官方订阅工具；claude-desktop 与 claude 共用同一凭据文件。"""
    if app_type in ("claude", "claude-desktop"):
        return "claude"
    if app_type == "codex":
        return "codex"
    if app_type == "gemini":
        return "gemini"
    return None


def collect(
    plan_id: str,
    tool: str,
    *,
    home: Path | None = None,
    timeout: int = TIMEOUT_DEFAULT,
    request=raw_request,
) -> QuotaResult:
    if tool == "claude":
        return collect_claude(plan_id, home=home, timeout=timeout, request=request)
    if tool == "codex":
        return collect_codex(plan_id, home=home, timeout=timeout, request=request)
    if tool == "gemini":
        return collect_gemini(plan_id, home=home, timeout=timeout, request=request)
    raise CollectorError("unsupported", f"官方订阅 {tool} 暂不支持")


def _login_error(message: str) -> CollectorError:
    return CollectorError("oauth_login_required", message)


def _http_check(status: int, tool_label: str) -> None:
    if status in (401, 403):
        raise _login_error(f"登录失效（HTTP {status}），请重新登录 {tool_label}")
    if status < 200 or status >= 300:
        raise CollectorError("fetch_failed", f"上游返回 HTTP {status}")


def _token_expired(value) -> bool:
    """凭据 expiresAt 是否已过期；兼容秒/毫秒时间戳与 ISO 字符串（无法解析视为未过期）。"""
    if value is None:
        return False
    now = time.time()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0:
            return False
        secs = value / 1000 if value >= 1e12 else value
        return secs < now
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return dt.timestamp() < now
    return False


# ---------------------------------------------------------------- Claude

def claude_credentials_path(home: Path | None = None) -> Path:
    """~/.claude/.credentials.json；CLAUDE_CONFIG_DIR 环境变量可覆盖。"""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override) if override else (home or Path.home()) / ".claude"
    return base / ".credentials.json"


def read_claude_credentials(path: Path | None = None):
    """返回 (access_token | None, status, message)；status: valid/expired/not_found/parse_error。"""
    path = path or claude_credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "not_found", "未找到 Claude CLI 登录凭据（~/.claude/.credentials.json）"
    except (OSError, ValueError) as exc:
        return None, "parse_error", f"Claude 凭据解析失败: {exc}"

    entry = data.get("claudeAiOauth") or data.get("claude.ai_oauth")
    if not isinstance(entry, dict):
        return None, "parse_error", "Claude 凭据文件无 claudeAiOauth 条目"
    token = entry.get("accessToken")
    if not isinstance(token, str) or not token:
        return None, "parse_error", "Claude 凭据缺少 accessToken"
    if _token_expired(entry.get("expiresAt")):
        return token, "expired", "Claude OAuth token 已过期，请重新登录 Claude CLI"
    return token, "valid", ""


def collect_claude(
    plan_id: str,
    *,
    home: Path | None = None,
    timeout: int = TIMEOUT_DEFAULT,
    request=raw_request,
) -> QuotaResult:
    token, status, message = read_claude_credentials(claude_credentials_path(home))
    if status in ("not_found", "parse_error"):
        raise _login_error(message or "未找到 Claude CLI 登录凭据")
    if status == "expired":
        try:
            return _query_claude(plan_id, token, timeout, request)
        except CollectorError as exc:
            # 仅 API 拒绝（401/403）才转登录失效；网络/解析错误原样上抛（§4.5）
            if exc.code == "oauth_login_required":
                raise _login_error(message or "Claude OAuth token 已过期") from None
            raise
    return _query_claude(plan_id, token, timeout, request)


def _query_claude(
    plan_id: str, token: str, timeout: int, request
) -> QuotaResult:
    status, _resp_headers, body = request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    _http_check(status, "Claude CLI")
    payload = parse_json_body(body)

    windows: list[QuotaWindow] = []
    now = time.time()
    for key, value in payload.items():
        if key == "extra_usage" or not isinstance(value, dict):
            continue
        utilization = parse_num(value.get("utilization"))
        if utilization is None:
            continue
        windows.append(
            QuotaWindow(
                key=key,
                label=_CLAUDE_TIER_LABELS.get(key, key),
                used_percent=clamp_percent(utilization),
                reset_in_sec=reset_to_seconds(value.get("resets_at"), now),
            )
        )
    if not windows:
        raise CollectorError("no_subscription", "该账号无有效套餐")

    extra = _claude_extra_text(payload.get("extra_usage"))
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        extra=extra,
        fetched_at=int(time.time()),
    )


def _claude_extra_text(extra_usage) -> str | None:
    if not isinstance(extra_usage, dict) or extra_usage.get("is_enabled") is not True:
        return None
    used = parse_num(extra_usage.get("used_credits"))
    limit = parse_num(extra_usage.get("monthly_limit"))
    parts: list[str] = []
    if used is not None and limit is not None:
        currency = extra_usage.get("currency") or "USD"
        parts.append(f"{used:.2f} / {limit:.2f} {currency}")
    utilization = parse_num(extra_usage.get("utilization"))
    if utilization is not None:
        parts.append(f"已用 {utilization:.0f}%")
    return "超额用量 " + " ".join(parts) if parts else "超额用量"


# ---------------------------------------------------------------- Codex

def codex_credentials_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".codex" / "auth.json"


def read_codex_credentials(path: Path | None = None):
    """返回 (access_token | None, account_id | None, status, message)。"""
    path = path or codex_credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None, "not_found", "未找到 Codex CLI 登录凭据（~/.codex/auth.json）"
    except (OSError, ValueError) as exc:
        return None, None, "parse_error", f"Codex 凭据解析失败: {exc}"

    if data.get("auth_mode") != "chatgpt":
        return None, None, "not_found", "Codex 未使用 OAuth 模式（auth_mode 非 chatgpt）"
    tokens = data.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token:
        return None, None, "parse_error", "Codex 凭据缺少 access_token"

    account_id = tokens.get("account_id")
    account_id = str(account_id) if account_id else None

    last_refresh = data.get("last_refresh")
    if isinstance(last_refresh, str) and _codex_token_stale(last_refresh):
        return token, account_id, "expired", "Codex token 可能已过期（>8 天未刷新），请重新登录 Codex CLI"
    return token, account_id, "valid", ""


def _codex_token_stale(last_refresh: str) -> bool:
    try:
        dt = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
    except ValueError:
        return False
    return time.time() - dt.timestamp() > 8 * 24 * 3600


def collect_codex(
    plan_id: str,
    *,
    home: Path | None = None,
    timeout: int = TIMEOUT_DEFAULT,
    request=raw_request,
) -> QuotaResult:
    token, account_id, status, message = read_codex_credentials(codex_credentials_path(home))
    if status in ("not_found", "parse_error"):
        raise _login_error(message or "未找到 Codex CLI 登录凭据")
    if status == "expired":
        try:
            return _query_codex(plan_id, token, account_id, timeout, request)
        except CollectorError as exc:
            # 仅 API 拒绝（401/403）才转登录失效；网络/解析错误原样上抛（§4.5）
            if exc.code == "oauth_login_required":
                raise _login_error(message or "Codex token 已过期") from None
            raise
    return _query_codex(plan_id, token, account_id, timeout, request)


def _window_name(limit_window_seconds) -> str:
    """Codex 窗口秒数 -> tier 名（与 CC Switch window_seconds_to_tier_name 一致）。"""
    secs = parse_num(limit_window_seconds)
    if secs is None:
        return "unknown"
    secs = int(secs)
    if secs == 18000:
        return "five_hour"
    if secs == 604800:
        return "seven_day"
    if secs == 2_592_000:
        return "30_day"
    hours = secs // 3600
    return f"{hours // 24}_day" if hours >= 24 else f"{hours}_hour"


def _window_label(name: str) -> str:
    if name == "five_hour":
        return "5小时"
    if name == "seven_day":
        return "每周"
    if name == "30_day":
        return "每月"
    if name.endswith("_day"):
        return f"{name[:-4]}天"
    if name.endswith("_hour"):
        return f"{name[:-5]}小时"
    return name


def _query_codex(
    plan_id: str, token: str, account_id: str | None, timeout: int, request
) -> QuotaResult:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "codex-cli",
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    status, _resp_headers, body = request(CODEX_USAGE_URL, headers=headers, timeout=timeout)
    _http_check(status, "Codex CLI")
    payload = parse_json_body(body)

    rate_limit = payload.get("rate_limit")
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    windows: list[QuotaWindow] = []
    now = time.time()
    for key in ("primary_window", "secondary_window"):
        win = rate_limit.get(key)
        if not isinstance(win, dict):
            continue
        used = parse_num(win.get("used_percent"))
        if used is None:
            continue
        name = _window_name(win.get("limit_window_seconds"))
        windows.append(
            QuotaWindow(
                key=name,
                label=_window_label(name),
                used_percent=clamp_percent(used),
                reset_in_sec=reset_to_seconds(win.get("reset_at"), now),
            )
        )
    if not windows:
        raise CollectorError("no_subscription", "该账号无有效套餐")
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        fetched_at=int(time.time()),
    )


# ---------------------------------------------------------------- Gemini

def gemini_credentials_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".gemini" / "oauth_creds.json"


def read_gemini_credentials(path: Path | None = None):
    """返回 (access_token | None, refresh_token | None, status, message)。"""
    path = path or gemini_credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None, "not_found", "未找到 Gemini CLI 登录凭据（~/.gemini/oauth_creds.json）"
    except (OSError, ValueError) as exc:
        return None, None, "parse_error", f"Gemini 凭据解析失败: {exc}"

    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        return None, None, "parse_error", "Gemini 凭据缺少 access_token"
    refresh_token = data.get("refresh_token")
    refresh_token = str(refresh_token) if isinstance(refresh_token, str) else None

    expiry = data.get("expiry_date")
    if isinstance(expiry, (int, float)) and not isinstance(expiry, bool):
        if expiry > 0 and expiry < time.time() * 1000:
            return token, refresh_token, "expired", "Gemini access token 已过期"
    return token, refresh_token, "valid", ""


def refresh_gemini_token(
    refresh_token: str,
    *,
    timeout: int = TIMEOUT_DEFAULT,
    request=raw_request,
) -> str | None:
    """refresh_token -> 新 access_token；任何失败返回 None（与 CC Switch 一致）。"""
    form = urllib.parse.urlencode(
        {
            "client_id": GEMINI_OAUTH_CLIENT_ID,
            "client_secret": GEMINI_OAUTH_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    try:
        status, _resp_headers, body = request(
            GEMINI_TOKEN_URL,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form,
            timeout=timeout,
        )
    except CollectorError:
        return None
    if status < 200 or status >= 300:
        return None
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    return str(token) if token else None


def collect_gemini(
    plan_id: str,
    *,
    home: Path | None = None,
    timeout: int = TIMEOUT_DEFAULT,
    request=raw_request,
) -> QuotaResult:
    token, refresh_token, status, message = read_gemini_credentials(
        gemini_credentials_path(home)
    )
    if status in ("not_found", "parse_error"):
        raise _login_error(message or "未找到 Gemini CLI 登录凭据")
    if status == "expired":
        if refresh_token:
            new_token = refresh_gemini_token(refresh_token, timeout=timeout, request=request)
            if new_token:
                return _query_gemini(plan_id, new_token, timeout, request)
        try:
            return _query_gemini(plan_id, token, timeout, request)
        except CollectorError as exc:
            # 仅 API 拒绝（401/403）才转登录失效；网络/解析错误原样上抛（§4.5）
            if exc.code == "oauth_login_required":
                raise _login_error(message or "Gemini OAuth token 已过期") from None
            raise
    return _query_gemini(plan_id, token, timeout, request)


def _gemini_request(
    url: str, token: str, body: dict, timeout: int, request
) -> tuple[int, bytes]:
    status, _resp_headers, body = request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        body=json.dumps(body).encode("utf-8"),
        timeout=timeout,
    )
    return status, body


def _extract_gemini_project(value) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("id", "projectId"):
            v = value.get(key)
            if isinstance(v, str) and v:
                return v
    return None


def _query_gemini(plan_id: str, token: str, timeout: int, request) -> QuotaResult:
    # Step 1: loadCodeAssist -> project id
    status, body = _gemini_request(
        GEMINI_LOAD_URL,
        token,
        {"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
        timeout,
        request,
    )
    _http_check(status, "Gemini CLI")
    load_payload = parse_json_body(body)
    project_id = _extract_gemini_project(load_payload.get("cloudaicompanionProject"))

    # Step 2: retrieveUserQuota -> buckets[]
    quota_body = {"project": project_id} if project_id else {}
    status, body = _gemini_request(GEMINI_QUOTA_URL, token, quota_body, timeout, request)
    _http_check(status, "Gemini CLI")
    quota_payload = parse_json_body(body)

    buckets = quota_payload.get("buckets")
    buckets = buckets if isinstance(buckets, list) else []

    # 按模型分类，同类取最紧张（remainingFraction 最小）；utilization = (1-剩余)×100
    best: dict[str, tuple[float, object]] = {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        model_id = bucket.get("modelId")
        category = _classify_gemini_model(str(model_id or "unknown"))
        remaining = parse_num(bucket.get("remainingFraction"))
        if remaining is None:
            continue
        remaining = min(max(remaining, 0.0), 1.0)
        current = best.get(category)
        if current is None or remaining < current[0]:
            best[category] = (remaining, bucket.get("resetTime"))

    now = time.time()
    windows: list[QuotaWindow] = []
    for category in sorted(best, key=_gemini_tier_order):
        remaining, reset_time = best[category]
        windows.append(
            QuotaWindow(
                key=category,
                label=_GEMINI_TIER_LABELS.get(category, category),
                used_percent=clamp_percent((1.0 - remaining) * 100.0),
                reset_in_sec=reset_to_seconds(reset_time, now),
            )
        )
    if not windows:
        raise CollectorError("no_subscription", "该账号无有效套餐")
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        fetched_at=int(time.time()),
    )


def _classify_gemini_model(model_id: str) -> str:
    if "flash-lite" in model_id:
        return "gemini_flash_lite"
    if "flash" in model_id:
        return "gemini_flash"
    if "pro" in model_id:
        return "gemini_pro"
    return model_id


def _gemini_tier_order(name: str) -> int:
    return {"gemini_pro": 0, "gemini_flash": 1, "gemini_flash_lite": 2}.get(name, 3)

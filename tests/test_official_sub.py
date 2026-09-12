"""官方订阅（Claude / Codex / Gemini OAuth）采集器契约测试（§10）。

凭据文件用 tmp_path 构造；配额接口 mock；端点/字段样例按 CC Switch
`src-tauri/src/services/subscription.rs`（2026-08）构造。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from token_widget.collectors import official_sub as os_
from token_widget.collectors.base import CollectorError

NOW = 1_785_000_000.0


def _respond(status: int = 200, payload=None, raw: bytes | None = None):
    resp_body = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def request(url, *, method="GET", headers=None, body=None, timeout=10, **kw):
        return status, {}, resp_body

    return request


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ================================================================ 凭据读取

def test_claude_credentials_valid(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-1", "expiresAt": int(time.time()) + 3600},
    })
    token, status, _ = os_.read_claude_credentials(
        os_.claude_credentials_path(tmp_path)
    )
    assert (token, status) == ("tok-1", "valid")


def test_claude_credentials_alt_key_and_expired(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claude.ai_oauth": {"accessToken": "tok-2", "expiresAt": int(time.time()) - 1},
    })
    token, status, msg = os_.read_claude_credentials(os_.claude_credentials_path(tmp_path))
    assert token == "tok-2"
    assert status == "expired"
    assert "过期" in msg


def test_claude_credentials_not_found(tmp_path) -> None:
    token, status, _ = os_.read_claude_credentials(os_.claude_credentials_path(tmp_path))
    assert (token, status) == (None, "not_found")


def test_codex_credentials_non_oauth_mode(tmp_path) -> None:
    _write(tmp_path / ".codex" / "auth.json", {"auth_mode": "api_key"})
    token, _, status, _ = os_.read_codex_credentials(os_.codex_credentials_path(tmp_path))
    assert (token, status) == (None, "not_found")


def test_codex_credentials_valid_with_account_id(tmp_path) -> None:
    _write(tmp_path / ".codex" / "auth.json", {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "codex-tok", "account_id": "acc-1"},
        # 动态时间戳（现在 - 1 天）：写死日期会在 >8 天阈值后变成时间炸弹
        "last_refresh": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    })
    token, account_id, status, _ = os_.read_codex_credentials(os_.codex_credentials_path(tmp_path))
    assert (token, account_id, status) == ("codex-tok", "acc-1", "valid")


def test_codex_credentials_stale(tmp_path) -> None:
    _write(tmp_path / ".codex" / "auth.json", {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "codex-tok"},
        "last_refresh": "2020-01-01T00:00:00+00:00",
    })
    _, _, status, msg = os_.read_codex_credentials(os_.codex_credentials_path(tmp_path))
    assert status == "expired"
    assert "8 天" in msg


def test_gemini_credentials_expired_by_ms(tmp_path) -> None:
    _write(tmp_path / ".gemini" / "oauth_creds.json", {
        "access_token": "gem-tok",
        "refresh_token": "refresh-1",
        "expiry_date": int(time.time()) * 1000 - 1,
    })
    token, refresh_token, status, _ = os_.read_gemini_credentials(
        os_.gemini_credentials_path(tmp_path)
    )
    assert (token, refresh_token, status) == ("gem-tok", "refresh-1", "expired")


# ================================================================ Claude

def test_claude_collect_uses_cli_credentials_and_maps_windows(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-1", "expiresAt": int(time.time()) + 3600},
    })
    payload = {
        "five_hour": {"utilization": 40, "resets_at": "2026-08-03T12:00:00+00:00"},
        "seven_day": {"utilization": 15, "resets_at": "2026-08-09T12:00:00+00:00"},
        "seven_day_opus": {"utilization": 0},
    }
    result = os_.collect_claude("p1", home=tmp_path, request=_respond(payload=payload))
    assert result.ok
    assert result.plan_id == "p1"
    keys = [w.key for w in result.windows]
    assert keys == ["five_hour", "seven_day", "seven_day_opus"]
    assert result.windows[0].label == "5小时"
    assert abs(result.windows[0].used_percent - 40.0) < 1e-6
    assert result.windows[0].reset_in_sec is not None


def test_claude_unknown_tier_and_extra_usage(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-1"},
    })
    payload = {
        "monthly": {"utilization": 55},
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 100.0,
            "used_credits": 12.5,
            "utilization": 12.5,
            "currency": "USD",
        },
    }
    result = os_.collect_claude("p1", home=tmp_path, request=_respond(payload=payload))
    assert [w.key for w in result.windows] == ["monthly"]
    assert result.extra and "12.50 / 100.00 USD" in result.extra


def test_claude_no_credentials_raises_login_required(tmp_path) -> None:
    with pytest.raises(CollectorError) as excinfo:
        os_.collect_claude("p1", home=tmp_path, request=_respond(payload={}))
    assert excinfo.value.code == "oauth_login_required"


def test_claude_expired_token_falls_back_to_api(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-exp", "expiresAt": int(time.time()) - 10},
    })
    payload = {"five_hour": {"utilization": 10}}
    result = os_.collect_claude("p1", home=tmp_path, request=_respond(payload=payload))
    assert result.ok  # 文件标记过期但 API 仍接受（与 CC Switch 一致）


def test_claude_api_401_maps_to_login_required(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-bad"},
    })
    with pytest.raises(CollectorError) as excinfo:
        os_.collect_claude("p1", home=tmp_path, request=_respond(status=401))
    assert excinfo.value.code == "oauth_login_required"


def test_claude_no_windows_maps_to_no_subscription(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-1"},
    })
    with pytest.raises(CollectorError) as excinfo:
        os_.collect_claude("p1", home=tmp_path, request=_respond(payload={}))
    assert excinfo.value.code == "no_subscription"


def test_claude_request_shape(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-1"},
    })
    seen = {}

    def request(url, *, headers=None, timeout=10, **kw):
        seen["url"] = url
        seen["headers"] = headers
        return 200, {}, json.dumps({"five_hour": {"utilization": 1}}).encode()

    os_.collect_claude("p1", home=tmp_path, request=request)
    assert seen["url"] == os_.CLAUDE_USAGE_URL
    assert seen["headers"]["Authorization"] == "Bearer tok-1"
    assert seen["headers"]["anthropic-beta"] == "oauth-2025-04-20"


# ================================================================ Codex

def test_codex_collect_maps_windows_and_window_names(tmp_path) -> None:
    _write(tmp_path / ".codex" / "auth.json", {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "codex-tok", "account_id": "acc-9"},
    })
    payload = {"rate_limit": {
        "primary_window": {"used_percent": 33, "limit_window_seconds": 18000,
                           "reset_at": int(time.time()) + 7200},
        "secondary_window": {"used_percent": 5, "limit_window_seconds": 2_592_000,
                             "reset_at": int(time.time()) + 86400 * 29},
    }}
    result = os_.collect_codex("p2", home=tmp_path, request=_respond(payload=payload))
    keys = [w.key for w in result.windows]
    assert keys == ["five_hour", "30_day"]
    assert result.windows[0].label == "5小时"
    assert result.windows[1].label == "每月"
    assert abs(result.windows[0].used_percent - 33.0) < 1e-6
    assert 7100 <= result.windows[0].reset_in_sec <= 7200


def test_codex_window_name_fallback() -> None:
    assert os_._window_name(3600) == "1_hour"
    assert os_._window_name(86400) == "1_day"
    assert os_._window_name(604800) == "seven_day"
    assert os_._window_name(None) == "unknown"
    assert os_._window_label("5_hour") == "5小时"
    assert os_._window_label("3_day") == "3天"


def test_codex_uses_account_id_header(tmp_path) -> None:
    seen = {}
    _write(tmp_path / ".codex" / "auth.json", {
        "auth_mode": "chatgpt", "tokens": {"access_token": "t", "account_id": "acc-9"},
    })

    def request(url, *, headers=None, timeout=10, **kw):
        seen["headers"] = headers
        return 200, {}, json.dumps({"rate_limit": {"primary_window": {
            "used_percent": 1, "limit_window_seconds": 18000}}}).encode()

    os_.collect_codex("p2", home=tmp_path, request=request)
    assert seen["headers"]["ChatGPT-Account-Id"] == "acc-9"
    assert seen["headers"]["User-Agent"] == "codex-cli"


def test_codex_non_oauth_raises_login_required(tmp_path) -> None:
    _write(tmp_path / ".codex" / "auth.json", {"auth_mode": "api_key"})
    with pytest.raises(CollectorError) as excinfo:
        os_.collect_codex("p2", home=tmp_path, request=_respond(payload={}))
    assert excinfo.value.code == "oauth_login_required"


# ================================================================ Gemini

def _gemini_creds(tmp_path: Path, expired: bool = False) -> None:
    _write(tmp_path / ".gemini" / "oauth_creds.json", {
        "access_token": "gem-tok",
        "refresh_token": "refresh-1" if expired else None,
        "expiry_date": int(time.time()) * 1000 - 1 if expired else int(time.time()) * 1000 + 3600_000,
    })


def _gemini_responder(quota_payload: dict):
    def request(url, *, method="GET", headers=None, body=None, timeout=10, **kw):
        if url == os_.GEMINI_LOAD_URL:
            return 200, {}, json.dumps({
                "cloudaicompanionProject": {"id": "proj-123"}
            }).encode()
        if url == os_.GEMINI_QUOTA_URL:
            return 200, {}, json.dumps(quota_payload).encode()
        raise AssertionError(f"unexpected url {url}")

    return request


def test_gemini_collect_groups_buckets_by_model(tmp_path) -> None:
    _gemini_creds(tmp_path)
    payload = {"buckets": [
        {"modelId": "gemini-2.5-pro", "remainingFraction": 0.5, "resetTime": "2026-08-03T10:00:00+00:00"},
        {"modelId": "gemini-2.5-flash", "remainingFraction": 0.8},
        {"modelId": "gemini-2.5-flash-lite", "remainingFraction": 0.9},
    ]}
    result = os_.collect_gemini("p3", home=tmp_path, request=_gemini_responder(payload))
    assert [w.key for w in result.windows] == ["gemini_pro", "gemini_flash", "gemini_flash_lite"]
    assert abs(result.windows[0].used_percent - 50.0) < 1e-6  # (1-0.5)×100
    assert result.windows[0].label == "Gemini Pro"
    assert result.windows[0].reset_in_sec is not None


def test_gemini_same_category_takes_most_used(tmp_path) -> None:
    _gemini_creds(tmp_path)
    payload = {"buckets": [
        {"modelId": "gemini-2.5-pro", "remainingFraction": 0.9},
        {"modelId": "gemini-2.5-pro-001", "remainingFraction": 0.2},
    ]}
    result = os_.collect_gemini("p3", home=tmp_path, request=_gemini_responder(payload))
    assert len(result.windows) == 1
    assert abs(result.windows[0].used_percent - 80.0) < 1e-6  # 取最小剩余 0.2


def test_gemini_expired_refreshes_token_then_queries(tmp_path) -> None:
    _gemini_creds(tmp_path, expired=True)
    calls = {"refresh": 0}

    def request(url, *, method="GET", headers=None, body=None, timeout=10, **kw):
        if url == os_.GEMINI_TOKEN_URL:
            calls["refresh"] += 1
            assert b"refresh_token=refresh-1" in body
            assert b"grant_type=refresh_token" in body
            return 200, {}, json.dumps({"access_token": "gem-tok-new"}).encode()
        if url == os_.GEMINI_LOAD_URL:
            assert headers["Authorization"] == "Bearer gem-tok-new"
            return 200, {}, json.dumps({"cloudaicompanionProject": "proj-1"}).encode()
        if url == os_.GEMINI_QUOTA_URL:
            return 200, {}, json.dumps({"buckets": [
                {"modelId": "gemini-2.5-pro", "remainingFraction": 0.0}
            ]}).encode()
        raise AssertionError(f"unexpected url {url}")

    result = os_.collect_gemini("p3", home=tmp_path, request=request)
    assert calls["refresh"] == 1
    assert result.ok and len(result.windows) == 1


def test_gemini_refresh_failure_falls_back_to_stale_token(tmp_path) -> None:
    _gemini_creds(tmp_path, expired=True)

    def request(url, *, method="GET", headers=None, body=None, timeout=10, **kw):
        if url == os_.GEMINI_TOKEN_URL:
            return 500, {}, b""
        if url == os_.GEMINI_LOAD_URL:
            return 200, {}, json.dumps({"cloudaicompanionProject": "proj-1"}).encode()
        if url == os_.GEMINI_QUOTA_URL:
            return 200, {}, json.dumps({"buckets": [
                {"modelId": "gemini-2.5-flash", "remainingFraction": 0.25}
            ]}).encode()
        raise AssertionError(f"unexpected url {url}")

    result = os_.collect_gemini("p3", home=tmp_path, request=request)
    assert result.ok and abs(result.windows[0].used_percent - 75.0) < 1e-6


def test_gemini_no_credentials_raises_login_required(tmp_path) -> None:
    with pytest.raises(CollectorError) as excinfo:
        os_.collect_gemini("p3", home=tmp_path, request=_respond(payload={}))
    assert excinfo.value.code == "oauth_login_required"


# ================================================================ 过期凭据的错误码保真
# 文件标记 expired 时仍先试 API：仅 API 拒绝（401/403）转 oauth_login_required，
# 网络/服务错误保持 fetch_failed，不误导用户重登。

def test_claude_expired_token_network_error_stays_fetch_failed(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-exp", "expiresAt": int(time.time()) - 10},
    })

    def request(url, *, headers=None, timeout=10, **kw):
        raise CollectorError("fetch_failed", "connection reset")

    with pytest.raises(CollectorError) as excinfo:
        os_.collect_claude("p1", home=tmp_path, request=request)
    assert excinfo.value.code == "fetch_failed"


def test_claude_expired_token_500_stays_fetch_failed(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-exp", "expiresAt": int(time.time()) - 10},
    })
    with pytest.raises(CollectorError) as excinfo:
        os_.collect_claude("p1", home=tmp_path, request=_respond(status=500))
    assert excinfo.value.code == "fetch_failed"


def test_claude_expired_token_401_maps_to_login_required(tmp_path) -> None:
    _write(tmp_path / ".claude" / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "tok-exp", "expiresAt": int(time.time()) - 10},
    })
    with pytest.raises(CollectorError) as excinfo:
        os_.collect_claude("p1", home=tmp_path, request=_respond(status=401))
    assert excinfo.value.code == "oauth_login_required"
    assert "过期" in (excinfo.value.message or "")


def test_codex_expired_token_500_stays_fetch_failed(tmp_path) -> None:
    _write(tmp_path / ".codex" / "auth.json", {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "codex-tok"},
        "last_refresh": "2020-01-01T00:00:00+00:00",
    })
    with pytest.raises(CollectorError) as excinfo:
        os_.collect_codex("p2", home=tmp_path, request=_respond(status=500))
    assert excinfo.value.code == "fetch_failed"


def test_codex_expired_token_401_maps_to_login_required(tmp_path) -> None:
    _write(tmp_path / ".codex" / "auth.json", {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "codex-tok"},
        "last_refresh": "2020-01-01T00:00:00+00:00",
    })
    with pytest.raises(CollectorError) as excinfo:
        os_.collect_codex("p2", home=tmp_path, request=_respond(status=401))
    assert excinfo.value.code == "oauth_login_required"


def test_gemini_expired_token_network_error_stays_fetch_failed(tmp_path) -> None:
    _gemini_creds(tmp_path, expired=True)

    def request(url, *, method="GET", headers=None, body=None, timeout=10, **kw):
        raise CollectorError("fetch_failed", "connection reset")

    with pytest.raises(CollectorError) as excinfo:
        os_.collect_gemini("p3", home=tmp_path, request=request)
    assert excinfo.value.code == "fetch_failed"


# ================================================================ 路由

def test_tool_for_app_type() -> None:
    assert os_.tool_for_app_type("claude") == "claude"
    assert os_.tool_for_app_type("claude-desktop") == "claude"
    assert os_.tool_for_app_type("codex") == "codex"
    assert os_.tool_for_app_type("gemini") == "gemini"
    assert os_.tool_for_app_type("opencode") is None


def test_collect_unknown_tool_raises_unsupported() -> None:
    with pytest.raises(CollectorError) as excinfo:
        os_.collect("p1", "whatever")
    assert excinfo.value.code == "unsupported"

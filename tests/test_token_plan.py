"""token_plan 模板（zhipu / zhipu_team / minimax / zenmux）契约测试（§10 采集器契约）。

端点与字段样例按 CC Switch `src-tauri/src/services/coding_plan.rs`（2026-08）构造；
纯映射函数用固定时钟，collect() 集成样例的 reset 时间基于 time.time()。
"""

from __future__ import annotations

import json
import time

import pytest

from token_widget import collectors as collectors_pkg
from token_widget.collectors import token_plan
from token_widget.collectors.base import CollectorError
from token_widget.config import WidgetConfig
from token_widget.models import Plan, QuotaResult

NOW = 1_785_000_000.0


def _respond(payload=None, status: int = 200, raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def request(url, *, headers=None, timeout=10, **kw):
        return status, {}, body

    return request


def _capture(payload=None):
    captured = {}
    body = json.dumps(payload).encode("utf-8") if payload is not None else b"{}"

    def request(url, *, headers=None, timeout=10, **kw):
        captured["url"] = url
        captured["headers"] = headers or {}
        return 200, {}, body

    return request, captured


# ================================================================ 智谱 GLM

def _zhipu_payload(now: float) -> dict:
    return {
        "success": True,
        "data": {
            "level": "GLM-4.6",
            "limits": [
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 3,
                    "percentage": 70.0,
                    "nextResetTime": int(now) * 1000 + 5 * 3600 * 1000,
                },
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 6,
                    "percentage": 30.0,
                    "nextResetTime": int(now) * 1000 + 6 * 86400 * 1000,
                },
            ],
        },
    }


def test_zhipu_full_response_maps_two_windows() -> None:
    now = time.time()
    windows, level = token_plan.map_zhipu(_zhipu_payload(now), now)
    assert [w.key for w in windows] == ["5h", "weekly"]
    assert abs(windows[0].used_percent - 70.0) < 1e-6
    assert abs(windows[1].used_percent - 30.0) < 1e-6
    assert 5 * 3600 - 2 <= windows[0].reset_in_sec <= 5 * 3600
    assert 6 * 86400 - 2 <= windows[1].reset_in_sec <= 6 * 86400
    assert level == "GLM-4.6"


def test_zhipu_old_plan_single_entry_falls_to_five_hour() -> None:
    payload = {
        "success": True,
        "data": {"limits": [{"type": "tokens_limit", "percentage": 55.0}]},
    }
    windows, level = token_plan.map_zhipu(payload, NOW)
    assert len(windows) == 1
    assert windows[0].key == "5h"
    assert abs(windows[0].used_percent - 55.0) < 1e-6
    assert windows[0].reset_in_sec is None


def test_zhipu_type_mismatch_is_skipped() -> None:
    payload = {
        "success": True,
        "data": {"limits": [{"type": "CHARGE_LIMIT", "unit": 3, "percentage": 10.0}]},
    }
    windows, _ = token_plan.map_zhipu(payload, NOW)
    assert windows == []


def test_zhipu_business_error_maps_to_upstream_error() -> None:
    with pytest.raises(CollectorError) as excinfo:
        token_plan.collect_zhipu(
            "p1", "key", "https://open.bigmodel.cn",
            request=_respond({"success": False, "msg": "ApiKey 已被删除或停用"}),
        )
    assert excinfo.value.code == "upstream_error"
    assert "ApiKey" in excinfo.value.message


def test_zhipu_request_shape_no_bearer_and_cn_host() -> None:
    ok = {"success": True, "data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 3, "percentage": 0}]}}
    request, captured = _capture(ok)
    token_plan.collect_zhipu(
        "p1", "zhipu-key", "https://open.bigmodel.cn/api/coding",
        request=request,
    )
    assert captured["url"].endswith("/api/monitor/usage/quota/limit")
    assert "open.bigmodel.cn" in captured["url"]
    assert captured["headers"]["Authorization"] == "zhipu-key"
    assert "Bearer" not in captured["headers"]["Authorization"]


def test_zhipu_en_host_by_base_url() -> None:
    assert token_plan.zhipu_quota_base("https://api.z.ai/v1") == token_plan.ZHIPU_EN_BASE
    assert (
        token_plan.zhipu_quota_base("https://open.bigmodel.cn/api")
        == token_plan.ZHIPU_CN_BASE
    )
    assert token_plan.zhipu_quota_base(None) == token_plan.ZHIPU_EN_BASE


def test_zhipu_team_request_carries_type2_and_org_project_headers() -> None:
    ok = {"success": True, "data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 3, "percentage": 0}]}}
    request, captured = _capture(ok)
    token_plan.collect_zhipu(
        "p1", "team-key", "https://open.bigmodel.cn",
        team_org="org-123", team_project="proj-456",
        request=request,
    )
    assert captured["url"] == (
        "https://open.bigmodel.cn/api/monitor/usage/quota/limit?type=2"
    )
    assert captured["headers"]["bigmodel-organization"] == "org-123"
    assert captured["headers"]["bigmodel-project"] == "proj-456"


def test_zhipu_team_missing_org_or_project_raises_auth_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        token_plan.collect_zhipu("p1", "key", None, team_org="org", request=_capture()[0])
    assert excinfo.value.code == "auth_failed"
    assert "组织 ID" in (excinfo.value.message or "")


# ================================================================ MiniMax

def _minimax_payload(now: float, weekly_status: int = 1) -> dict:
    return {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "model_remains": [
            {
                "model_name": "video",
                "current_interval_remaining_percent": 20.0,
            },
            {
                "model_name": "general",
                "current_interval_remaining_percent": 30.0,
                "end_time": int(now) * 1000 + 3 * 3600 * 1000,
                "current_weekly_status": weekly_status,
                "current_weekly_remaining_percent": 60.0,
                "weekly_end_time": int(now) * 1000 + 3 * 86400 * 1000,
            },
        ],
    }


def test_minimax_general_entry_maps_two_windows_skips_video() -> None:
    now = time.time()
    windows = token_plan.map_minimax(_minimax_payload(now), now)
    assert [w.key for w in windows] == ["5h", "weekly"]
    assert abs(windows[0].used_percent - 70.0) < 1e-6  # 100 - 剩余30
    assert abs(windows[1].used_percent - 40.0) < 1e-6  # 100 - 剩余60
    assert 3 * 3600 - 2 <= windows[0].reset_in_sec <= 3 * 3600


def test_minimax_weekly_inactive_when_status_not_1() -> None:
    now = time.time()
    windows = token_plan.map_minimax(_minimax_payload(now, weekly_status=3), now)
    assert [w.key for w in windows] == ["5h"]


def test_minimax_no_general_entry_returns_empty() -> None:
    payload = {"base_resp": {"status_code": 0}, "model_remains": [{"model_name": "video"}]}
    assert token_plan.map_minimax(payload, NOW) == []


def test_minimax_business_error_uses_status_msg() -> None:
    with pytest.raises(CollectorError) as excinfo:
        token_plan.collect_minimax(
            "p1", "key", "https://api.minimaxi.com",
            request=_respond({
                "base_resp": {"status_code": 1002, "status_msg": "invalid token"},
            }),
        )
    assert excinfo.value.code == "upstream_error"
    assert "invalid token" in excinfo.value.message


def test_minimax_host_selection() -> None:
    assert token_plan.minimax_host("https://api.minimaxi.com/v1") == token_plan.MINIMAX_CN_HOST
    assert token_plan.minimax_host("https://api.minimax.io") == token_plan.MINIMAX_EN_HOST
    assert token_plan.minimax_host(None) == token_plan.MINIMAX_EN_HOST


def test_minimax_request_uses_bearer() -> None:
    ok = {
        "base_resp": {"status_code": 0},
        "model_remains": [
            {"model_name": "general", "current_interval_remaining_percent": 100.0}
        ],
    }
    request, captured = _capture(ok)
    token_plan.collect_minimax("p1", "mm-key", "https://api.minimax.io", request=request)
    assert "api.minimax.io" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer mm-key"


# ================================================================ ZenMux

def _zenmux_payload() -> dict:
    return {
        "success": True,
        "data": {
            "plan": {"tier": "pro"},
            "account_status": "active",
            "quota_5_hour": {
                "usage_percentage": 0.42,
                "resets_at": "2026-08-03T10:00:00+00:00",
                "used_value_usd": 2.10,
                "max_value_usd": 5.00,
            },
            "quota_7_day": {
                "usage_percentage": 0.1,
                "resets_at": "2026-08-09T10:00:00+00:00",
                "used_value_usd": 3.5,
                "max_value_usd": 35.0,
            },
        },
    }


def test_zenmux_maps_two_windows_with_usd_and_plan_info() -> None:
    windows, info = token_plan.map_zenmux(_zenmux_payload(), NOW)
    assert [w.key for w in windows] == ["5h", "weekly"]  # §2 语义名，与其他采集器统一
    assert abs(windows[0].used_percent - 42.0) < 1e-6  # 0.42 × 100
    assert windows[0].used_text == "$2.10 / $5.00"
    assert windows[0].reset_in_sec is not None
    assert abs(windows[1].used_percent - 10.0) < 1e-6
    assert info == "pro (active)"


def test_zenmux_collect_uses_base_url_directly() -> None:
    ok = {"success": True, "data": {"quota_5_hour": {"usage_percentage": 0.0}}}
    request, captured = _capture(ok)
    token_plan.collect_zenmux(
        "p1", "zx-key", "https://zenmux.example.com/quota",
        request=request,
    )
    assert captured["url"] == "https://zenmux.example.com/quota"
    assert captured["headers"]["Authorization"] == "Bearer zx-key"


def test_zenmux_business_error_maps_to_upstream_error() -> None:
    with pytest.raises(CollectorError) as excinfo:
        token_plan.collect_zenmux(
            "p1", "key", "https://zenmux.example.com",
            request=_respond({"success": False, "message": "quota disabled"}),
        )
    assert excinfo.value.code == "upstream_error"
    assert "quota disabled" in excinfo.value.message


def test_zenmux_missing_base_url_raises_auth_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        token_plan.collect_zenmux("p1", "key", None)
    assert excinfo.value.code == "auth_failed"


# ================================================================ 公共错误映射

@pytest.mark.parametrize("provider", ["zhipu", "minimax", "zenmux"])
def test_auth_errors_map_to_auth_failed(provider: str) -> None:
    for status in (401, 403):
        with pytest.raises(CollectorError) as excinfo:
            if provider == "zhipu":
                token_plan.collect_zhipu(
                    "p1", "key", "https://open.bigmodel.cn",
                    request=_respond(status=status),
                )
            elif provider == "minimax":
                token_plan.collect_minimax(
                    "p1", "key", "https://api.minimaxi.com",
                    request=_respond(status=status),
                )
            else:
                token_plan.collect_zenmux(
                    "p1", "key", "https://zenmux.example.com",
                    request=_respond(status=status),
                )
        assert excinfo.value.code == "auth_failed"


@pytest.mark.parametrize("provider", ["zhipu", "minimax", "zenmux"])
def test_server_error_maps_to_fetch_failed(provider: str) -> None:
    with pytest.raises(CollectorError) as excinfo:
        if provider == "zhipu":
            token_plan.collect_zhipu(
                "p1", "key", "https://open.bigmodel.cn",
                request=_respond(status=500),
            )
        elif provider == "minimax":
            token_plan.collect_minimax(
                "p1", "key", "https://api.minimaxi.com",
                request=_respond(status=500),
            )
        else:
            token_plan.collect_zenmux(
                "p1", "key", "https://zenmux.example.com",
                request=_respond(status=500),
            )
    assert excinfo.value.code == "fetch_failed"


def test_invalid_json_maps_to_parse_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        token_plan.collect_zhipu(
            "p1", "key", "https://open.bigmodel.cn",
            request=_respond(raw=b"<html>not json</html>"),
        )
    assert excinfo.value.code == "parse_failed"


def test_http_error_message_contains_request_url() -> None:
    """fetch_failed 的 message 带请求 URL（排障用）。"""
    with pytest.raises(CollectorError) as excinfo:
        token_plan.collect_minimax(
            "p1", "key", "https://api.minimaxi.com",
            request=_respond(status=500),
        )
    assert excinfo.value.code == "fetch_failed"
    assert "api.minimaxi.com" in (excinfo.value.message or "")


# ================================================================ 路由（collectors.__init__）

def _zhipu_plan(provider: str, **us_extra) -> Plan:
    return Plan(
        id="p-zhipu",
        app_type="claude",
        name="智谱 GLM",
        category=None,
        icon=None,
        icon_color=None,
        is_current=False,
        quota_kind="token_plan",
        settings_config={"env": {
            "ANTHROPIC_AUTH_TOKEN": "zhipu-key",
            "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn",
        }},
        usage_script={"codingPlanProvider": provider, **us_extra},
    )


def _capture_zhipu(monkeypatch) -> dict:
    captured = {}

    def fake_collect_zhipu(
        plan_id, api_key, base_url, *, team_org=None, team_project=None, **kw
    ):
        captured.update(
            plan_id=plan_id, api_key=api_key, base_url=base_url,
            team_org=team_org, team_project=team_project,
        )
        return QuotaResult(plan_id=plan_id, ok=True)

    monkeypatch.setattr(token_plan, "collect_zhipu", fake_collect_zhipu)
    return captured


def test_route_zhipu_personal_ignores_leftover_team_fields(monkeypatch) -> None:
    """provider=zhipu 但 usage_script 残留 teamOrganizationId：不打团队端点。"""
    captured = _capture_zhipu(monkeypatch)
    plan = _zhipu_plan("zhipu", teamOrganizationId="org-x", teamProjectId="proj-y")
    collector = collectors_pkg.build_collector(plan, WidgetConfig())
    assert collector is not None
    collector()
    assert captured["team_org"] is None
    assert captured["team_project"] is None


def test_route_zhipu_team_passes_org_and_project(monkeypatch) -> None:
    """provider=zhipu_team：透传组织/项目 ID（§4.3.2 团队端点）。"""
    captured = _capture_zhipu(monkeypatch)
    plan = _zhipu_plan("zhipu_team", teamOrganizationId="org-x", teamProjectId="proj-y")
    collector = collectors_pkg.build_collector(plan, WidgetConfig())
    assert collector is not None
    collector()
    assert captured["team_org"] == "org-x"
    assert captured["team_project"] == "proj-y"


def test_route_by_base_url_is_case_insensitive() -> None:
    assert collectors_pkg._route_by_base_url("HTTPS://OPEN.BIGMODEL.CN/api") == "zhipu"
    assert collectors_pkg._route_by_base_url("https://API.Z.AI") == "zhipu"
    assert collectors_pkg._route_by_base_url("") is None

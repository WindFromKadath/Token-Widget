"""Kimi 采集器测试：mock 响应 -> QuotaResult 字段断言（§10 契约）。"""

from __future__ import annotations

import json
import time

import pytest

from token_widget.collectors import kimi
from token_widget.collectors.base import CollectorError
from token_widget.credentials import Credentials

# 纯映射函数（map_kimi）用固定时钟；collect() 内部用真实时钟，故集成样例
# 的 resetTime 基于 time.time() 构造。
NOW = 1_785_000_000.0


def _respond(payload: dict, status: int = 200):
    def request(url, *, headers=None, timeout=10, **kw):
        assert url == kimi.USAGE_URL
        assert headers["Authorization"].startswith("Bearer ")
        return status, {}, json.dumps(payload).encode("utf-8")

    return request


def test_full_response_maps_two_windows() -> None:
    now = time.time()
    payload = {
        "limits": [
            {"detail": {
                "limit": 100, "remaining": 30,
                "resetTime": int(now) + 3600,
            }},
        ],
        "usage": {"limit": 1000, "remaining": 900, "resetTime": int(now) + 86400},
    }
    result = kimi.collect("p1", Credentials(api_key="k"), request=_respond(payload))
    assert result.ok
    keys = [w.key for w in result.windows]
    assert keys == ["5h", "weekly"]
    five_hour, weekly = result.windows
    assert abs(five_hour.used_percent - 70.0) < 1e-6
    assert 3590 <= five_hour.reset_in_sec <= 3600
    assert abs(weekly.used_percent - 10.0) < 1e-6


def test_reset_time_variants() -> None:
    # 毫秒时间戳（>=1e12）与 ISO 字符串
    iso = "2026-08-03T12:00:00+00:00"
    payload = {
        "limits": [{"detail": {
            "limit": 10, "remaining": 5,
            "resetTime": int((NOW + 7200) * 1000),
        }}],
        "usage": {"limit": 10, "remaining": 5, "resetTime": iso},
    }
    windows = kimi.map_kimi(payload, NOW)
    assert len(windows) == 2
    assert 7195 <= windows[0].reset_in_sec <= 7200
    from datetime import datetime

    expected = datetime.fromisoformat(iso).timestamp() - NOW
    assert abs(windows[1].reset_in_sec - expected) <= 1


def test_negative_or_zero_reset_time_is_none() -> None:
    payload = {"usage": {"limit": 10, "remaining": 10, "resetTime": -1}}
    windows = kimi.map_kimi(payload, NOW)
    assert windows and windows[0].reset_in_sec is None
    assert windows[0].used_percent == 0.0


def test_empty_payload_raises_parse_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        kimi.collect("p1", Credentials(api_key="k"), request=_respond({}))
    assert excinfo.value.code == "parse_failed"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors(status: int) -> None:
    with pytest.raises(CollectorError) as excinfo:
        kimi.collect("p1", Credentials(api_key="k"),
                     request=_respond({}, status=status))
    assert excinfo.value.code == "auth_failed"


def test_server_error_maps_to_fetch_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        kimi.collect("p1", Credentials(api_key="k"),
                     request=_respond({}, status=500))
    assert excinfo.value.code == "fetch_failed"


def test_missing_api_key() -> None:
    with pytest.raises(CollectorError) as excinfo:
        kimi.collect("p1", Credentials())
    assert excinfo.value.code == "auth_failed"

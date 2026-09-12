"""余额采集器契约测试（templateType == "balance"，§4.7）。

端点与响应样例按 CC Switch `src-tauri/src/services/balance.rs`（2026-08）构造。
"""

from __future__ import annotations

import json

import pytest

from token_widget.collectors import balance
from token_widget.collectors.base import CollectorError


def _respond(payload: dict, status: int = 200):
    def request(url, *, headers=None, timeout=10, **kw):
        return status, {}, json.dumps(payload).encode("utf-8")

    return request


# ---------------------------------------------------------------- DeepSeek

def test_deepseek_maps_balance_per_currency() -> None:
    payload = {
        "is_available": True,
        "balance_infos": [
            {"currency": "CNY", "total_balance": "119.00",
             "granted_balance": "0.00", "topped_up_balance": "119.00"},
            {"currency": "USD", "total_balance": "5.50"},
        ],
    }
    result = balance.collect(
        "deepseek-id", "sk-ds", "https://api.deepseek.com",
        request=_respond(payload),
    )
    assert result.ok
    assert len(result.windows) == 2
    assert result.windows[0].label == "余额 CNY"
    assert result.windows[0].used_percent is None  # 余额类无百分比
    assert result.windows[0].used_text == "¥119.00"
    assert result.windows[1].used_text == "$5.50"
    assert result.worst is None  # 余额窗口不参与最紧张排序


def test_deepseek_unavailable_adds_extra() -> None:
    payload = {"is_available": False, "balance_infos": [
        {"currency": "CNY", "total_balance": "0.00"},
    ]}
    result = balance.collect("p", "k", "https://api.deepseek.com", request=_respond(payload))
    assert result.ok and result.extra == "余额不足"


def test_deepseek_missing_balance_info_raises_parse_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        balance.collect("p", "k", "https://api.deepseek.com", request=_respond({}))
    assert excinfo.value.code == "parse_failed"


# ---------------------------------------------------------------- 其他供应商

def test_stepfun() -> None:
    payload = {"object": "account", "type": "balance", "balance": "88.50"}
    result = balance.collect("p", "k", "https://api.stepfun.com/v1", request=_respond(payload))
    assert result.ok and result.windows[0].used_text == "¥88.50"


def test_siliconflow_cn_and_en() -> None:
    payload = {"code": 0, "data": {"totalBalance": 12.34}}
    result = balance.collect("p", "k", "https://api.siliconflow.cn/v1", request=_respond(payload))
    assert result.windows[0].used_text == "¥12.34"
    result = balance.collect("p", "k", "https://api.siliconflow.com", request=_respond(payload))
    assert result.windows[0].used_text == "$12.34"


def test_openrouter_computes_remaining_and_percent() -> None:
    payload = {"data": {"total_credits": 10.0, "total_usage": 4.0}}
    result = balance.collect("p", "k", "https://openrouter.ai/api/v1", request=_respond(payload))
    assert result.windows[0].used_text == "$6.00"
    assert abs(result.windows[0].used_percent - 40.0) < 1e-6


def test_novita_divides_by_10000() -> None:
    payload = {"availableBalance": 123400}
    result = balance.collect("p", "k", "https://api.novita.ai", request=_respond(payload))
    assert result.windows[0].used_text == "$12.34"


# ---------------------------------------------------------------- 路由与错误

def test_unknown_provider_raises_unsupported() -> None:
    with pytest.raises(CollectorError) as excinfo:
        balance.collect("p", "k", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert excinfo.value.code == "unsupported"
    assert "dashscope" in (excinfo.value.message or "")


def test_missing_api_key_raises_auth_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        balance.collect("p", None, "https://api.deepseek.com")
    assert excinfo.value.code == "auth_failed"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors(status: int) -> None:
    with pytest.raises(CollectorError) as excinfo:
        balance.collect("p", "k", "https://api.deepseek.com", request=_respond({}, status=status))
    assert excinfo.value.code == "auth_failed"


def test_server_error_maps_to_fetch_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        balance.collect("p", "k", "https://api.deepseek.com", request=_respond({}, status=500))
    assert excinfo.value.code == "fetch_failed"


def test_invalid_json_maps_to_parse_failed() -> None:
    def request(url, *, headers=None, timeout=10, **kw):
        return 200, {}, b"not json"

    with pytest.raises(CollectorError) as excinfo:
        balance.collect("p", "k", "https://api.deepseek.com", request=request)
    assert excinfo.value.code == "parse_failed"

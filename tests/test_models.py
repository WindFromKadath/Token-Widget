"""模型层测试：quota_kind 路由与余额窗口语义。"""

from __future__ import annotations

from token_widget.models import (
    QuotaResult,
    QuotaWindow,
    classify_quota_kind,
    fmt_duration_sec,
    percent_color_class,
)


def test_classify_balance_template() -> None:
    us = {"enabled": True, "templateType": "balance", "autoQueryInterval": 5}
    assert classify_quota_kind("cn_official", us) == "balance"


def test_classify_priority_and_fallbacks() -> None:
    assert classify_quota_kind("third_party", {"enabled": True, "templateType": "token_plan"}) == "token_plan"
    assert classify_quota_kind("official", {"enabled": True}) == "official_sub"
    assert classify_quota_kind("third_party", None) == "consumption_only"
    assert classify_quota_kind("third_party", {"enabled": False, "templateType": "balance"}) == "consumption_only"


def test_worst_skips_balance_windows() -> None:
    result = QuotaResult(
        plan_id="p1",
        ok=True,
        windows=[
            QuotaWindow(key="balance", label="余额", used_percent=None, used_text="¥10.00"),
            QuotaWindow(key="5h", label="5小时", used_percent=30.0),
            QuotaWindow(key="weekly", label="每周", used_percent=80.0),
        ],
    )
    assert result.worst is not None
    assert result.worst.key == "weekly"


def test_worst_none_when_all_balance() -> None:
    result = QuotaResult(
        plan_id="p1",
        ok=True,
        windows=[
            QuotaWindow(key="balance", label="余额", used_percent=None, used_text="¥10.00"),
        ],
    )
    assert result.worst is None


def test_fmt_duration_sec() -> None:
    assert fmt_duration_sec(None) == "?"
    assert fmt_duration_sec(-100) == "0m"
    assert fmt_duration_sec(86400) == "1d"
    assert fmt_duration_sec(5 * 3600 + 3 * 60) == "5h3m"


def test_percent_color_class_boundaries() -> None:
    """§6.1 分档边界：<70 绿 / 70-89 橙 / >=90 红。"""
    assert percent_color_class(69) == "green"
    assert percent_color_class(70) == "orange"
    assert percent_color_class(89) == "orange"
    assert percent_color_class(90) == "red"


def test_classify_custom_script_via_code_without_template() -> None:
    """templateType=None + code -> custom_script（自定义脚本）。"""
    us = {"enabled": True, "code": "({})"}
    assert classify_quota_kind("third_party", us) == "custom_script"
    us = {"enabled": True, "templateType": None, "code": "({})"}
    assert classify_quota_kind("third_party", us) == "custom_script"


def test_classify_token_plan_beats_official_category() -> None:
    """enabled + token_plan 优先于 category=official 兜底。"""
    us = {"enabled": True, "templateType": "token_plan"}
    assert classify_quota_kind("official", us) == "token_plan"

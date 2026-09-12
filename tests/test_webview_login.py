"""Webview 登录的纯函数测试（M3 §4.4 凭据捕获逻辑）。

对话框本身依赖 QtWebEngine，不做自动单测（真机验证）；此处覆盖
cookie/workspace 捕获的判定与提取逻辑。
"""

from __future__ import annotations

from token_widget.ui.webview_login import extract_workspace_id, is_auth_cookie


def test_extract_workspace_id() -> None:
    assert (
        extract_workspace_id("https://opencode.ai/workspace/wrk_00000000000000000000000000/go")
        == "wrk_00000000000000000000000000"
    )
    assert (
        extract_workspace_id("https://opencode.ai/workspace/wrk_abc123")
        == "wrk_abc123"
    )


def test_extract_workspace_id_ignores_other_urls() -> None:
    assert extract_workspace_id("https://opencode.ai/") is None
    assert extract_workspace_id("https://auth.opencode.ai/login") is None
    assert extract_workspace_id("https://example.com/workspace/wrk_1") is None
    assert extract_workspace_id("not a url") is None


def test_is_auth_cookie_matches_open_code_domains() -> None:
    assert is_auth_cookie("auth", "opencode.ai")
    assert is_auth_cookie("auth", ".opencode.ai")
    assert is_auth_cookie("auth", "https://opencode.ai") is False


def test_is_auth_cookie_rejects_subdomain_placeholder() -> None:
    """auth.opencode.ai 授权页的匿名占位 cookie（子域）必须排除。"""
    assert not is_auth_cookie("auth", "auth.opencode.ai")
    assert not is_auth_cookie("auth", ".auth.opencode.ai")


def test_is_auth_cookie_rejects_other_names_and_domains() -> None:
    assert not is_auth_cookie("session", "opencode.ai")
    assert not is_auth_cookie("auth", "example.com")
    assert not is_auth_cookie("auth", "notopencode.ai")
    assert not is_auth_cookie("auth", "")
    assert not is_auth_cookie("", "opencode.ai")

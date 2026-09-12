"""OpenCode Go 解析测试：四类金样本（§10，fixtures 来自参考实现）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from token_widget.collectors import opencode_go
from token_widget.collectors.base import CollectorError
from token_widget.config import OpencodeGoAccount

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_inline_sample_parses_three_windows() -> None:
    usages = opencode_go.parse_usage(_read("inline.html"))
    assert set(usages) == {"rolling", "weekly", "monthly"}
    for u in usages.values():
        assert isinstance(u["percent"], int)
        assert 0 <= u["percent"] <= 100
    # 内联样本应带 resetInSec
    assert usages["rolling"]["reset_in_sec"] is not None


def test_dom_sample_parses_three_windows() -> None:
    usages = opencode_go.parse_usage(_read("dom.html"))
    assert len(usages) == 3
    for u in usages.values():
        assert 0 <= u["percent"] <= 100


def test_login_page_raises_cookie_expired() -> None:
    html = _read("login.html")

    def fake_request(url, *, headers=None, timeout=10, **kw):
        return 200, {"Content-Type": "text/html"}, html.encode("utf-8")

    account = OpencodeGoAccount(auth_cookie="fake", workspace_id="wrk_fake")
    with pytest.raises(CollectorError) as excinfo:
        opencode_go.fetch_html(account, request=fake_request)
    assert excinfo.value.code == "cookie_expired"


def test_no_subscription_detected() -> None:
    assert opencode_go.is_no_subscription(_read("no_subscription.html"))
    assert not opencode_go.is_no_subscription(_read("inline.html"))


def test_collect_outputs_three_quota_windows() -> None:
    html = _read("inline.html")

    def fake_request(url, *, headers=None, timeout=10, **kw):
        assert "wrk_" in url
        assert headers["Cookie"].startswith("auth=fake")
        return 200, {}, html.encode("utf-8")

    account = OpencodeGoAccount(auth_cookie="fake", workspace_id="wrk_test")
    result = opencode_go.collect("plan-1", account, request=fake_request)
    assert result.ok
    assert [w.key for w in result.windows] == ["5h", "weekly", "monthly"]
    assert result.plan_label == "OpenCode Go"


def test_collect_no_subscription_error() -> None:
    html = _read("no_subscription.html")

    def fake_request(url, *, headers=None, timeout=10, **kw):
        return 200, {}, html.encode("utf-8")

    account = OpencodeGoAccount(auth_cookie="fake", workspace_id="wrk_test")
    with pytest.raises(CollectorError) as excinfo:
        opencode_go.collect("plan-1", account, request=fake_request)
    assert excinfo.value.code == "no_subscription"


def test_redirect_to_login_counts_as_expired() -> None:
    """302 到 auth.opencode.ai -> cookie_expired（§4.4 重定向判定）。

    响应头键用小写 location（raw_request 契约：头名统一小写）。
    """
    calls = []

    def fake_request(url, *, headers=None, timeout=10, **kw):
        calls.append(url)
        if len(calls) == 1:
            return 302, {"location": "https://auth.opencode.ai/login"}, b""
        return 200, {}, b"<html><title>OpenAuth</title></html>"

    account = OpencodeGoAccount(auth_cookie="fake", workspace_id="wrk_test")
    with pytest.raises(CollectorError) as excinfo:
        opencode_go.fetch_html(account, request=fake_request)
    assert excinfo.value.code == "cookie_expired"


def test_redirect_subdomain_keeps_cookie() -> None:
    """跳转到 opencode.ai 子域（auth.opencode.ai）时仍附带 auth Cookie。"""
    html = _read("inline.html")
    calls = []

    def fake_request(url, *, headers=None, timeout=10, **kw):
        calls.append((url, headers))
        if len(calls) == 1:
            return 302, {"location": "https://auth.opencode.ai/verify"}, b""
        if len(calls) == 2:
            return 302, {"location": "https://opencode.ai/workspace/wrk_test/go"}, b""
        return 200, {}, html.encode("utf-8")

    account = OpencodeGoAccount(auth_cookie="fake", workspace_id="wrk_test")
    assert opencode_go.fetch_html(account, request=fake_request) == html
    assert len(calls) == 3
    assert calls[1][1]["Cookie"].startswith("auth=fake")  # 子域仍带 Cookie


def test_redirect_cross_domain_drops_cookie() -> None:
    """跳转到外部域时去 Cookie（§9：凭据不外发第三方）。"""
    html = _read("inline.html")
    calls = []

    def fake_request(url, *, headers=None, timeout=10, **kw):
        calls.append((url, headers))
        if len(calls) == 1:
            return 302, {"location": "https://evil.example.com/phish"}, b""
        return 200, {}, html.encode("utf-8")

    account = OpencodeGoAccount(auth_cookie="fake", workspace_id="wrk_test")
    assert opencode_go.fetch_html(account, request=fake_request) == html
    assert len(calls) == 2
    assert calls[0][1]["Cookie"].startswith("auth=fake")  # 首跳带 Cookie
    assert "Cookie" not in calls[1][1]  # 跨域跳转已去除

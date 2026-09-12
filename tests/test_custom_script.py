"""custom_script 执行器测试（§10）：占位符、映射、Node runner 集成。"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from token_widget.collectors import custom_script
from token_widget.collectors.base import CollectorError, redact_secrets
from token_widget.credentials import Credentials

CREDS = Credentials(
    api_key="sk-test",
    base_url="https://base.example.com",
    access_token="tok-1",
    user_id="user-1",
)


# ---------------------------------------------------------------- 占位符

def test_placeholder_variants() -> None:
    code = "a={{apiKey}} b={{ apiKey }} c={{api_key}} u={{baseUrl}}" \
           " v={{ base_url }} t={{accessToken}} i={{userId}}"
    out = custom_script.substitute_placeholders(code, CREDS)
    assert out == (
        "a=sk-test b=sk-test c=sk-test u=https://base.example.com"
        " v=https://base.example.com t=tok-1 i=user-1"
    )


# ---------------------------------------------------------------- 映射

def test_map_total_remaining() -> None:
    out = {"planName": "套餐A", "remaining": 3, "total": 10, "unit": "USD"}
    result = custom_script.map_extractor_output("p1", out)
    assert result.ok and len(result.windows) == 1
    w = result.windows[0]
    assert abs(w.used_percent - 70.0) < 1e-6
    assert w.label == "套餐A"
    assert w.used_text == "$7.00 / $10.00"


def test_map_used_total_and_percent_unit() -> None:
    assert custom_script.map_extractor_output(
        "p1", {"used": 25, "total": 100}
    ).windows[0].used_percent == 25.0
    assert custom_script.map_extractor_output(
        "p1", {"used": 42, "unit": "%"}
    ).windows[0].used_percent == 42.0


def test_map_percent_unit_remaining_only() -> None:
    """unit="%" 且只有 remaining：按剩余百分比换算为已用（100 - remaining）。"""
    result = custom_script.map_extractor_output("p1", {"remaining": 30, "unit": "%"})
    assert result.ok and len(result.windows) == 1
    assert abs(result.windows[0].used_percent - 70.0) < 1e-6


def test_map_remaining_only_becomes_balance_window() -> None:
    out = {"planName": "余额", "remaining": 4.5, "unit": "USD"}
    result = custom_script.map_extractor_output("p1", out)
    assert result.ok and len(result.windows) == 1
    w = result.windows[0]
    assert w.used_percent is None
    assert w.used_text == "$4.50"
    assert w.key == "balance"


def test_map_unmappable_raises_parse_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        custom_script.map_extractor_output("p1", {"foo": 1})
    assert excinfo.value.code == "parse_failed"


def test_map_array_multiple_windows() -> None:
    out = [
        {"planName": "A", "used": 10, "total": 100},
        {"planName": "B", "used": 90, "total": 100, "extra": "快满了"},
    ]
    result = custom_script.map_extractor_output("p1", out, label="供应商")
    assert result.ok and len(result.windows) == 2
    assert [w.label for w in result.windows] == ["A", "B"]
    assert result.extra == "快满了"


def test_map_invalid_false() -> None:
    out = {"isValid": False, "invalidMessage": "Key 已过期"}
    result = custom_script.map_extractor_output("p1", out)
    assert not result.ok and result.error is None
    assert result.message == "Key 已过期"


def test_map_unusable_raises_parse_failed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        custom_script.map_extractor_output("p1", {"foo": 1})
    assert excinfo.value.code == "parse_failed"


def test_reset_in_sec_passthrough() -> None:
    out = {"used": 1, "total": 10, "resetInSec": 3600}
    assert custom_script.map_extractor_output("p1", out).windows[0].reset_in_sec == 3600


def test_map_percent_zero_kept() -> None:
    """percent=0（刚重置的窗口）不再被 `or` 合并丢弃。"""
    result = custom_script.map_extractor_output("p1", {"percent": 0})
    assert result.ok and len(result.windows) == 1
    assert result.windows[0].used_percent == 0.0


def test_reset_in_sec_zero_kept() -> None:
    """resetInSec=0 是合法值（刚重置），不被 `or` 合并吞掉。"""
    out = {"used": 1, "total": 10, "resetInSec": 0}
    assert custom_script.map_extractor_output("p1", out).windows[0].reset_in_sec == 0


def test_redact_secrets_masks_known_key_styles() -> None:
    """§9：sk-*、Bearer 值、Google Key 脱敏为 ***。"""
    assert redact_secrets("401 for sk-abc123def456") == "401 for ***"
    assert redact_secrets("Authorization: Bearer tok-abcdefgh123") == "Authorization: ***"
    assert redact_secrets("key=AIza" + "a" * 35) == "key=***"
    assert redact_secrets("普通错误信息") == "普通错误信息"


# ---------------------------------------------------------------- Node 集成

pytestmark_node = pytest.mark.skipif(
    custom_script.node_path() is None, reason="未检测到 Node.js"
)


class _JsonHandler(http.server.BaseHTTPRequestHandler):
    payload: dict = {}
    delay: float = 0.0

    def do_GET(self) -> None:  # noqa: N802
        import time

        time.sleep(self.delay)
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:  # noqa: D102
        pass


@pytest.fixture
def local_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _script(url: str, extractor_body: str) -> str:
    return (
        "({ request: { url: '" + url + "', method: 'GET' },"
        " extractor: function(response) { " + extractor_body + " } })"
    )


@pytestmark_node
def test_node_runner_end_to_end(local_server) -> None:
    _JsonHandler.payload = {"left": 30, "cap": 100}
    code = _script(local_server, "return { remaining: response.left, total: response.cap, planName: 'E2E' };")
    result = custom_script.collect("p1", code, CREDS, timeout_sec=10, label="集成")
    assert result.ok
    assert abs(result.windows[0].used_percent - 70.0) < 1e-6
    assert result.windows[0].label == "E2E"


@pytestmark_node
def test_node_runner_timeout(local_server) -> None:
    _JsonHandler.payload = {}
    _JsonHandler.delay = 3.0
    code = _script(local_server, "return {};")
    try:
        with pytest.raises(CollectorError) as excinfo:
            custom_script.collect("p1", code, CREDS, timeout_sec=1)
        assert excinfo.value.code == "fetch_failed"
    finally:
        _JsonHandler.delay = 0.0


@pytestmark_node
def test_node_runner_script_error(local_server) -> None:
    _JsonHandler.payload = {}
    code = _script(local_server, "throw new Error('boom');")
    with pytest.raises(CollectorError) as excinfo:
        custom_script.collect("p1", code, CREDS, timeout_sec=10)
    assert excinfo.value.code == "fetch_failed"  # 非零退出 -> fetch_failed（§4.2）


@pytestmark_node
def test_node_runner_stderr_redacted(local_server) -> None:
    """stderr 含 sk-xxx 时 message 已脱敏（§9：不落密钥）。"""
    _JsonHandler.payload = {}
    code = _script(local_server, "throw new Error('401 for sk-abc123def456ghi');")
    with pytest.raises(CollectorError) as excinfo:
        custom_script.collect("p1", code, CREDS, timeout_sec=10)
    assert excinfo.value.code == "fetch_failed"
    message = excinfo.value.message or ""
    assert "sk-abc123def456ghi" not in message
    assert "***" in message

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge.py 的单元测试：解析、格式化、端到端 HTTP（mock 抓取）。运行: python test_bridge.py"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import bridge

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParser(unittest.TestCase):
    def test_inline_three_sections(self):
        usages = bridge.parse_usage(read_fixture("inline.html"))
        self.assertEqual(set(usages), {"rolling", "weekly", "monthly"})
        self.assertEqual(usages["rolling"].percent, 12)
        self.assertEqual(usages["rolling"].reset_in_sec, 18000)
        self.assertEqual(usages["weekly"].percent, 45)
        self.assertEqual(usages["monthly"].percent, 67)
        self.assertEqual(usages["monthly"].reset_in_sec, 1036800)

    def test_inline_skips_null_duplicate(self):
        # monthlyUsage:null 出现在真正的用量块之后，不应覆盖已解析结果
        html = (
            'monthlyUsage: $R[1] = { status: "ok", resetInSec: 100, usagePercent: 5 }\n'
            "other: { monthlyUsage: null }\n"
        )
        usages = bridge.parse_inline(html)
        self.assertEqual(usages["monthly"].percent, 5)

    def test_dom_fallback(self):
        usages = bridge.parse_usage(read_fixture("dom.html"))
        self.assertEqual(usages["rolling"].percent, 23)
        self.assertEqual(usages["weekly"].percent, 56)
        self.assertEqual(usages["monthly"].percent, 78)
        self.assertEqual(usages["weekly"].reset_text, "重置于 2 天 4 小时")

    def test_merged_inline_priority(self):
        # 内联只有 rolling，DOM 补齐 weekly/monthly
        html = (
            'rollingUsage: $R[9] = { status: "ok", resetInSec: 60, usagePercent: 7 }\n'
            + read_fixture("dom.html")
        )
        usages = bridge.parse_usage(html)
        self.assertEqual(usages["rolling"].percent, 7)  # 内联优先
        self.assertEqual(usages["weekly"].percent, 56)  # DOM 补齐

    def test_no_subscription(self):
        self.assertTrue(bridge.is_no_subscription(read_fixture("no_subscription.html")))
        self.assertFalse(bridge.is_no_subscription(read_fixture("inline.html")))


class TestFormat(unittest.TestCase):
    def test_fmt_reset(self):
        self.assertEqual(bridge.fmt_reset(bridge.Usage(0, reset_in_sec=18000)), "5h")
        self.assertEqual(bridge.fmt_reset(bridge.Usage(0, reset_in_sec=198000)), "2d7h")
        self.assertEqual(bridge.fmt_reset(bridge.Usage(0, reset_in_sec=600)), "10m")
        self.assertEqual(
            bridge.fmt_reset(bridge.Usage(0, reset_text="重置于 2 天")), "重置于 2 天"
        )
        self.assertEqual(bridge.fmt_reset(bridge.Usage(0)), "?")

    def test_build_data(self):
        usages = bridge.parse_usage(read_fixture("inline.html"))
        data = bridge.build_data(usages)
        self.assertEqual(data, "滚动 12% (5h) | 周 45% (2d7h) | 月 67% (12d)")


class TestServer(unittest.TestCase):
    """端到端：起真实 HTTP 服务，mock fetch_html 返回 fixture。"""

    @classmethod
    def setUpClass(cls):
        cls.cfg = {
            "server": {"host": "127.0.0.1", "port": 0, "api_token": "test-token"},
            "fetch": {"timeout": 5, "retries": 0},
            "cache_ttl": 300,
            "default_account": "main",
            "accounts": {"main": {"auth_cookie": "x", "workspace_id": "wrk_x"}},
        }
        cls.html = read_fixture("inline.html")
        cls.orig_fetch = bridge.fetch_html
        cls.orig_save = bridge.save_config
        bridge.fetch_html = lambda account, settings: cls.html
        bridge.save_config = lambda cfg: None  # 测试不落盘
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        cls.httpd.cfg = cls.cfg
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        bridge.fetch_html = cls.orig_fetch
        bridge.save_config = cls.orig_save
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path: str, token: str | None = "test-token") -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def setUp(self):
        bridge._CACHE.clear()

    def test_health(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_usage_success(self):
        status, body = self.get("/usage")
        self.assertEqual(status, 200)
        self.assertTrue(body["success"])
        self.assertEqual(body["usage"]["rolling"]["percent"], 12)
        self.assertIn("滚动 12%", body["data"])
        self.assertFalse(body["cached"])

    def test_usage_cache(self):
        self.get("/usage")
        _, body = self.get("/usage")
        self.assertTrue(body["cached"])

    def test_usage_force_refresh(self):
        self.get("/usage")
        _, body = self.get("/usage?refresh=1")
        self.assertFalse(body["cached"])

    def test_unauthorized(self):
        status, body = self.get("/usage", token="wrong")
        self.assertEqual(status, 401)
        self.assertFalse(body["success"])

    def test_unknown_account(self):
        status, body = self.get("/usage/ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["reason"], "no_account")

    def test_auth_expired(self):
        bridge.fetch_html = lambda account, settings: (_ for _ in ()).throw(
            bridge.AuthExpiredError("cookie_expired")
        )
        try:
            _, body = self.get("/usage?refresh=1")
            self.assertFalse(body["success"])
            self.assertEqual(body["reason"], "cookie_expired")
        finally:
            bridge.fetch_html = lambda account, settings: self.__class__.html

    def test_stale_fallback_on_network_error(self):
        self.get("/usage")  # 先建立缓存
        bridge.fetch_html = lambda account, settings: (_ for _ in ()).throw(
            bridge.FetchError("无法连接")
        )
        try:
            _, body = self.get("/usage?refresh=1")
            self.assertTrue(body["success"])  # 返回旧数据而不是报错
            self.assertTrue(body.get("stale"))
        finally:
            bridge.fetch_html = lambda account, settings: self.__class__.html

    def test_post_config_updates_cookie(self):
        length_body = json.dumps({"account": "main", "auth_cookie": "new-cookie"})
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/config",
            data=length_body.encode("utf-8"),
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        self.assertTrue(body["success"])
        self.assertEqual(
            self.cfg["accounts"]["main"]["auth_cookie"], "new-cookie"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

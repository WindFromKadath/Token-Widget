#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCode Go 用量桥接服务（零第三方依赖，Python 3.10+ 标准库）

作用：抓取 opencode.ai 工作区 Go 套餐页面（SSR HTML），解析 5 小时 / 每周 / 每月
三档用量百分比，以 JSON API 形式提供给 CC Switch「自定义用量脚本」或其他工具。

用法：
    1. 复制 config.example.json 为 config.json，填入 auth_cookie 与 workspace_id
    2. python bridge.py
    3. GET  http://127.0.0.1:18443/health
       GET  http://127.0.0.1:18443/usage           （默认账号）
       GET  http://127.0.0.1:18443/usage/<account> （指定账号）
       POST http://127.0.0.1:18443/config          （更新 cookie，见 README）

解析逻辑参考自 MIT 协议项目 andywang425/opencode-go-usage-api 的 parser.py，
特此致谢。本实现改用纯标准库，便于在 Windows 上免依赖运行。
"""

from __future__ import annotations

import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
DEFAULT_DATA_TEMPLATE = (
    "滚动 {rolling_percent}% ({rolling_reset}) | "
    "周 {weekly_percent}% ({weekly_reset}) | "
    "月 {monthly_percent}% ({monthly_reset})"
)
LOGIN_PAGE_MARKER = "<title>OpenAuth</title>"
MAX_REDIRECTS = 5


# ---------------------------------------------------------------- 数据模型

@dataclass(frozen=True)
class Usage:
    """单项用量：已用百分比 + 距重置秒数（内联路径）或重置原文（DOM 兜底）。"""

    percent: int
    reset_in_sec: int | None = None
    status: str | None = None
    reset_text: str | None = None


@dataclass
class Account:
    account_id: str
    auth_cookie: str
    workspace_id: str

    @property
    def workspace_url(self) -> str:
        return f"https://opencode.ai/workspace/{self.workspace_id}/go"


class FetchError(Exception):
    """网络、超时或上游非 200。"""


class AuthExpiredError(FetchError):
    """被重定向到登录页：cookie 失效，或 workspace_id 错误（如用了工作区名称而非 wrk_ 前缀的 ID）。"""


class NoSubscriptionError(FetchError):
    """账号无 Go 订阅。"""


class ParseError(FetchError):
    """页面结构变化，三项用量均未解析出来。"""


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"未找到配置文件 {CONFIG_PATH}\n"
            "请复制 config.example.json 为 config.json 并填入 auth_cookie / workspace_id"
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("accounts"):
        raise SystemExit("config.json 中 accounts 不能为空")
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(CONFIG_PATH)


def get_account(cfg: dict, account_id: str | None) -> Account | None:
    accounts = cfg.get("accounts", {})
    aid = account_id or cfg.get("default_account") or next(iter(accounts))
    raw = accounts.get(aid)
    if not raw:
        return None
    return Account(aid, raw.get("auth_cookie", ""), raw.get("workspace_id", ""))


# ---------------------------------------------------------------- 抓取

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _raw_get(url: str, cookie: str, timeout: int, user_agent: str) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html",
            "Cookie": cookie,
        },
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        # 30x 因禁止自动重定向也会落到这里，由调用方处理
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def fetch_html(account: Account, settings: dict) -> str:
    """手动跟随重定向抓取工作区页面；凭据每次请求显式携带。"""
    timeout = int(settings.get("timeout", 10))
    retries = int(settings.get("retries", 1))
    user_agent = settings.get("user_agent", DEFAULT_USER_AGENT)
    locale = settings.get("locale", "zh")
    cookie = f"auth={account.auth_cookie}; oc_locale={locale}"
    try:
        cookie.encode("latin-1")
        account.workspace_url.encode("ascii")
    except UnicodeEncodeError:
        raise FetchError(
            "auth_cookie / workspace_id 含有非 ASCII 字符——请确认只粘贴了"
            " cookie 的值部分（一长串字母数字）和形如 wrk_xxx 的工作区 ID"
        )

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(0.5)
        try:
            url = account.workspace_url
            status, html = 0, ""
            for _ in range(MAX_REDIRECTS):
                status, headers, body = _raw_get(url, cookie, timeout, user_agent)
                if status in (301, 302, 303, 307, 308) and headers.get("Location"):
                    url = urllib.parse.urljoin(url, headers["Location"])
                    continue
                html = body.decode("utf-8", errors="replace")
                break
            else:
                raise FetchError("重定向次数过多")

            final = urllib.parse.urlparse(url)
            if (
                final.hostname == "auth.opencode.ai"
                or (final.hostname == "opencode.ai" and final.path.startswith("/auth"))
                or LOGIN_PAGE_MARKER in html
            ):
                raise AuthExpiredError("cookie_expired")
            if status != 200:
                raise FetchError(f"上游返回 HTTP {status}")
            return html
        except (AuthExpiredError, FetchError):
            raise
        except Exception as exc:  # 网络层异常（超时/连接失败）可重试
            last_exc = exc
    raise FetchError(f"无法连接 OpenCode（超时或上游异常）：{last_exc}")


# ---------------------------------------------------------------- 解析
# 内联 hydration 数据形如：
#   rollingUsage: $R[31] = { status: "ok", resetInSec: 18000, usagePercent: 0 }
# 值块非严格 JSON（可能含 !0、new Date() 等），逐字段正则抽取。

_USAGE_KEYS = {"rolling": "rollingUsage", "weekly": "weeklyUsage", "monthly": "monthlyUsage"}
_USAGE_VALUE_RE = r"\s*:\s*(?:\$R\[\d+\]\s*=\s*)?\{"


def _extract_usage_block(html: str, key: str) -> str | None:
    """截出 `<key>:` 之后的 {...} 块（跳过 `:null` 这类非用量重复出现）。"""
    m = re.search(re.escape(key) + _USAGE_VALUE_RE, html)
    if not m:
        return None
    brace_start = m.end() - 1
    depth = 0
    for i in range(brace_start, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[brace_start : i + 1]
    return None


def _parse_int_field(block: str, field: str) -> int | None:
    m = re.search(re.escape(field) + r"\s*:\s*(-?\d+)", block)
    return int(m.group(1)) if m else None


def _parse_str_field(block: str, field: str) -> str | None:
    m = re.search(re.escape(field) + r'\s*:\s*"([^"]*)"', block)
    return m.group(1) if m else None


def parse_inline(html: str) -> dict[str, Usage]:
    result: dict[str, Usage] = {}
    for name, key in _USAGE_KEYS.items():
        block = _extract_usage_block(html, key)
        if not block:
            continue
        percent = _parse_int_field(block, "usagePercent")
        if percent is None:
            continue
        result[name] = Usage(
            percent=percent,
            reset_in_sec=_parse_int_field(block, "resetInSec"),
            status=_parse_str_field(block, "status"),
        )
    return result


_DOM_ORDER = ("rolling", "weekly", "monthly")
_USAGE_ITEM_RE = re.compile(r'data-slot="usage-item"')


def _clean_reset_text(raw: str) -> str | None:
    cleaned = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def parse_dom(html: str) -> dict[str, Usage]:
    """DOM 兜底：3 个 data-slot="usage-item" 块，顺序 rolling → weekly → monthly。"""
    result: dict[str, Usage] = {}
    starts = [m.start() for m in _USAGE_ITEM_RE.finditer(html)]
    for idx, name in enumerate(_DOM_ORDER):
        if idx >= len(starts):
            break
        seg_start = starts[idx]
        seg_end = starts[idx + 1] if idx + 1 < len(starts) else seg_start + 800
        segment = html[seg_start:seg_end]
        m = re.search(
            r'data-slot="usage-value">\s*(?:<!--.*?-->)?\s*(\d+)', segment, re.S
        ) or re.search(r"width:\s*(\d+)%", segment)
        if not m:
            continue
        rt = re.search(r'data-slot="reset-time">\s*(.*?)</span>', segment, re.S)
        result[name] = Usage(
            percent=int(m.group(1)),
            reset_text=_clean_reset_text(rt.group(1)) if rt else None,
        )
    return result


def parse_usage(html: str) -> dict[str, Usage]:
    """内联优先，缺失项用 DOM 补齐。"""
    inline = parse_inline(html)
    if len(inline) == 3:
        return inline
    dom = parse_dom(html)
    merged = dict(dom)
    merged.update(inline)
    return merged


def is_no_subscription(html: str) -> bool:
    """无 Go 订阅页：存在订阅按钮且 billing 块 lite / liteSubscriptionID 为 null。"""
    if 'data-slot="subscribe-button"' not in html:
        return False
    return bool(re.search(r"lite\s*:\s*null", html)) and bool(
        re.search(r"liteSubscriptionID\s*:\s*null", html)
    )


# ---------------------------------------------------------------- 格式化

def fmt_reset(u: Usage) -> str:
    sec = u.reset_in_sec
    if sec is None:
        return u.reset_text or "?"
    sec = max(sec, 0)
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def build_data(usages: dict[str, Usage], template: str = DEFAULT_DATA_TEMPLATE) -> str:
    values: dict[str, str] = {}
    for section in ("rolling", "weekly", "monthly"):
        u = usages.get(section)
        values[f"{section}_percent"] = str(u.percent) if u else "—"
        values[f"{section}_reset"] = fmt_reset(u) if u else "—"
        values[f"{section}_status"] = (u.status or "—") if u else "—"
    try:
        return template.format_map(values)
    except (KeyError, ValueError, IndexError):
        return DEFAULT_DATA_TEMPLATE.format_map(values)


def usage_to_json(usages: dict[str, Usage]) -> dict:
    return {
        name: {
            "percent": u.percent,
            "reset_in_sec": u.reset_in_sec,
            "reset_text": u.reset_text,
            "status": u.status,
            "reset_in": fmt_reset(u),
        }
        for name, u in usages.items()
    }


# ---------------------------------------------------------------- 查询（带缓存）

_CACHE: dict[str, tuple[float, dict]] = {}


def query_account(cfg: dict, account: Account, force_refresh: bool = False) -> dict:
    fetch_cfg = cfg.get("fetch", {})
    ttl = int(cfg.get("cache_ttl", 300))
    now = time.time()

    if not force_refresh and ttl > 0:
        hit = _CACHE.get(account.account_id)
        if hit and now - hit[0] < ttl:
            return {**hit[1], "cached": True}

    try:
        html = fetch_html(account, fetch_cfg)
        if is_no_subscription(html):
            raise NoSubscriptionError("no_subscription")
        usages = parse_usage(html)
        if not usages:
            raise ParseError("parse_empty")
        result = {
            "success": True,
            "reason": "",
            "data": build_data(usages, cfg.get("data_template", DEFAULT_DATA_TEMPLATE)),
            "usage": usage_to_json(usages),
            "account": account.account_id,
            "fetched_at": int(now),
        }
        _CACHE[account.account_id] = (now, result)
        return {**result, "cached": False}
    except AuthExpiredError:
        _CACHE.pop(account.account_id, None)
        return {
            "success": False,
            "reason": "cookie_expired",
            "data": "登录凭证已失效，或 workspace_id 不正确"
            "（应为 URL 中 wrk_ 前缀的 ID，不是工作区名称）。"
            "请检查 config.json 后重试（支持 POST /config 热更新）",
        }
    except NoSubscriptionError:
        return {"success": False, "reason": "no_subscription", "data": "该账号无 Go 订阅"}
    except ParseError:
        return {
            "success": False,
            "reason": "parse_empty",
            "data": "页面结构可能已变化，未能解析出用量，请检查桥接服务版本",
        }
    except FetchError as exc:
        stale = _CACHE.get(account.account_id)
        if stale:  # 网络抖动时回退旧数据，避免 CC Switch 卡片报错
            return {**stale[1], "cached": True, "stale": True}
        return {"success": False, "reason": "fetch_error", "data": str(exc)}


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "OpenCodeGoBridge/1.0"

    # 安静一点，不走 stderr 默认日志；需要排障时取消下一行注释
    # def log_message(self, fmt, *args): super().log_message(fmt, *args)
    def log_message(self, fmt, *args):  # noqa: D102
        pass

    def _cfg(self) -> dict:
        return self.server.cfg  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        token = self._cfg().get("server", {}).get("api_token", "")
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(
            auth.encode("utf-8"), f"Bearer {token}".encode("utf-8")
        )

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self._send_json({"status": "ok"})
            return

        if path == "/usage" or path.startswith("/usage/"):
            if not self._authorized():
                self._send_json(
                    {"success": False, "reason": "unauthorized", "data": "未授权"}, 401
                )
                return
            account_id = path.split("/", 2)[2] if path.startswith("/usage/") else None
            account = get_account(self._cfg(), account_id)
            if account is None:
                self._send_json(
                    {"success": False, "reason": "no_account", "data": "账号不存在"}, 404
                )
                return
            force = query.get("refresh", ["0"])[0] in ("1", "true")
            self._send_json(query_account(self._cfg(), account, force))
            return

        self._send_json({"success": False, "reason": "not_found", "data": ""}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") != "/config":
            self._send_json({"success": False, "reason": "not_found", "data": ""}, 404)
            return
        if not self._authorized():
            self._send_json(
                {"success": False, "reason": "unauthorized", "data": "未授权"}, 401
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json(
                {"success": False, "reason": "bad_json", "data": "请求体不是合法 JSON"}, 400
            )
            return

        account_id = body.get("account") or self._cfg().get("default_account")
        cfg = self._cfg()
        target = cfg.get("accounts", {}).get(account_id)
        if target is None:
            self._send_json(
                {"success": False, "reason": "no_account", "data": "账号不存在"}, 404
            )
            return
        if body.get("auth_cookie"):
            target["auth_cookie"] = body["auth_cookie"]
        if body.get("workspace_id"):
            target["workspace_id"] = body["workspace_id"]
        save_config(cfg)
        _CACHE.pop(account_id, None)  # 凭据变更后立刻失效旧缓存
        self._send_json({"success": True, "data": f"账号 {account_id} 已更新"})


def main() -> None:
    cfg = load_config()
    server_cfg = cfg.get("server", {})
    host = server_cfg.get("host", "127.0.0.1")
    port = int(server_cfg.get("port", 18443))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.cfg = cfg  # type: ignore[attr-defined]
    print(f"OpenCode Go 桥接服务已启动: http://{host}:{port}")
    print("端点: GET /health | GET /usage | GET /usage/<account> | POST /config")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

"""挂件自有配置（DESIGN.md §7）—— 挂件唯一的自有持久化数据。

含 OpenCode cookie 等凭据字段：禁止随日志输出；文件原子写入。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ENV_OVERRIDE = "TOKEN_WIDGET_CONFIG"


@dataclass
class OpencodeGoAccount:
    auth_cookie: str = ""
    workspace_id: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.auth_cookie and self.workspace_id)


@dataclass
class WidgetConfig:
    selected_plan_ids: list[str] = field(default_factory=list)
    window_x: int | None = None
    window_y: int | None = None
    window_w: int | None = None
    window_h: int | None = None
    always_on_top: bool = True
    refresh_enabled: bool = True
    theme: str = "dark"  # dark / light / glass（theme.THEMES）
    opencode_go_accounts: dict[str, OpencodeGoAccount] = field(default_factory=dict)
    default_opencode_account: str = "main"

    @property
    def default_opencode_account_data(self) -> OpencodeGoAccount | None:
        return self.opencode_go_accounts.get(self.default_opencode_account)


def config_path() -> Path:
    """挂件目录下的 config.json；允许环境变量覆盖（测试用）。

    开发期 = 项目根；PyInstaller 打包后 = exe 同目录（M3）。
    """
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.json"
    return Path(__file__).resolve().parent.parent / "config.json"


def load(path: Path | None = None) -> WidgetConfig:
    """缺失/损坏时回退默认配置，不崩溃。"""
    path = path or config_path()
    cfg = WidgetConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg

    ids = raw.get("selectedPlanIds")
    if isinstance(ids, list):
        cfg.selected_plan_ids = [str(i) for i in ids]
    win = raw.get("window")
    win = win if isinstance(win, dict) else {}
    if isinstance(win.get("x"), int):
        cfg.window_x = win["x"]
    if isinstance(win.get("y"), int):
        cfg.window_y = win["y"]
    if isinstance(win.get("w"), int) and win["w"] > 0:
        cfg.window_w = win["w"]
    if isinstance(win.get("h"), int) and win["h"] > 0:
        cfg.window_h = win["h"]
    cfg.always_on_top = bool(win.get("alwaysOnTop", True))
    ui = raw.get("ui")
    ui = ui if isinstance(ui, dict) else {}
    if isinstance(ui.get("theme"), str):
        cfg.theme = ui["theme"]
    refresh = raw.get("refresh")
    refresh = refresh if isinstance(refresh, dict) else {}
    cfg.refresh_enabled = bool(refresh.get("globalEnabled", True))

    go = raw.get("opencodeGo")
    go = go if isinstance(go, dict) else {}
    accounts = go.get("accounts")
    accounts = accounts if isinstance(accounts, dict) else {}
    for name, item in accounts.items():
        if not isinstance(item, dict):
            continue
        cfg.opencode_go_accounts[str(name)] = OpencodeGoAccount(
            auth_cookie=str(item.get("auth_cookie", "")),
            workspace_id=str(item.get("workspace_id", "")),
        )
    if isinstance(go.get("default"), str):
        cfg.default_opencode_account = go["default"]
    return cfg


def save(cfg: WidgetConfig, path: Path | None = None) -> None:
    """原子写入；目标被占用时重试后降级直写——任何 OSError 都不向上抛。

    调用点均为 Qt 槽且无捕获：配置丢失可接受，崩溃不可接受。
    """
    path = path or config_path()
    payload = {
        "selectedPlanIds": cfg.selected_plan_ids,
        "window": {
            "x": cfg.window_x,
            "y": cfg.window_y,
            "w": cfg.window_w,
            "h": cfg.window_h,
            "alwaysOnTop": cfg.always_on_top,
        },
        "ui": {"theme": cfg.theme},
        "refresh": {"globalEnabled": cfg.refresh_enabled},
        "opencodeGo": {
            "accounts": {
                name: {
                    "auth_cookie": acc.auth_cookie,
                    "workspace_id": acc.workspace_id,
                }
                for name, acc in cfg.opencode_go_accounts.items()
            },
            "default": cfg.default_opencode_account,
        },
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(3):
            try:
                tmp.replace(path)
                return
            except OSError:
                if attempt < 2:  # Windows 杀软/索引器占用：退避 100ms 重试
                    time.sleep(0.1)
        # replace 仍失败（只读目标等）：降级直接写目标文件
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

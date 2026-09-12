"""UI 层回归测试（offscreen）：托盘菜单契约、置顶同步、行渲染细节、暂停状态回读。

需要 QApplication 的用例统一在 QT_QPA_PLATFORM=offscreen 下运行；
webview 对话框本身依赖 QtWebEngine，不在此覆盖（真机验证）。
"""

from __future__ import annotations

import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from token_widget.config import WidgetConfig
from token_widget.models import Plan, QuotaResult, QuotaWindow
from token_widget.store import Store
from token_widget.ui.app import Application
from token_widget.ui.context import AppContext
from token_widget.ui.main_window import MainWindow
from token_widget.ui.plan_row import PlanRow
from token_widget.ui.theme import current as current_theme
from token_widget.ui.tray import TrayIcon


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _plan(plan_id: str = "p1", name: str = "测试套餐", selected: bool = True) -> Plan:
    return Plan(
        id=plan_id, app_type="claude", name=name, category=None,
        icon=None, icon_color=None, is_current=False,
        quota_kind="custom_script", selected=selected,
    )


# ------------------------------------------------------------ 托盘（§6.3）

def test_tray_menu_contract(qapp) -> None:
    """§6.3 菜单契约：刷新全部/选择套餐…/主题/暂停自动刷新/置顶/退出。"""
    tray = TrayIcon()
    texts = [
        a.text() for a in tray.contextMenu().actions() if not a.isSeparator()
    ]
    assert texts == ["刷新全部", "选择套餐…", "主题", "暂停自动刷新", "置顶", "退出"]
    assert tray.action_pause.isCheckable()
    assert tray.action_pin.isCheckable()


def test_tray_pin_toggled_signal(qapp) -> None:
    tray = TrayIcon()
    emitted: list[bool] = []
    tray.pin_toggled.connect(emitted.append)
    tray.action_pin.setChecked(True)
    assert emitted == [True]


# ------------------------------------------------------------ 主窗置顶同步

def test_set_always_on_top_syncs_button_and_signal(qapp) -> None:
    cfg = WidgetConfig(always_on_top=False)
    win = MainWindow(cfg)
    emitted: list[bool] = []
    win.pin_changed.connect(emitted.append)

    win.set_always_on_top(True)
    assert cfg.always_on_top is True
    assert win.btn_pin.isChecked()
    assert emitted == [True]

    win.set_always_on_top(True)  # 幂等：同值不再发信号、不重设 flags
    assert emitted == [True]


# ------------------------------------------------------------ 窗口位置恢复

def test_restore_geometry_keeps_onscreen_position(qapp) -> None:
    geo = qapp.primaryScreen().availableGeometry()
    x, y = geo.left() + 10, geo.top() + 10
    win = MainWindow(WidgetConfig(window_x=x, window_y=y))
    assert (win.x(), win.y()) == (x, y)


def test_restore_geometry_clamps_offscreen_position(qapp) -> None:
    """坐标落在已断开的副屏：clamp 回主屏可用区域（无边框窗无法拖回）。"""
    win = MainWindow(WidgetConfig(window_x=-99999, window_y=-99999))
    geo = qapp.primaryScreen().availableGeometry()
    assert geo.left() <= win.x() <= geo.right() - win.width() + 1
    assert geo.top() <= win.y() <= geo.bottom() - win.height() + 1


# ------------------------------------------------------------ Plan 行渲染

def test_render_error_resets_window_label_style(qapp) -> None:
    """unsupported（灰）迁移到 error 后，错误文本不得残留灰色样式。"""
    row = PlanRow(_plan())
    row.set_result(QuotaResult(
        plan_id="p1", ok=False, error="unsupported", message="仅消耗",
        fetched_at=int(time.time()),
    ))
    assert current_theme().fg_stale in row.window_label.styleSheet()
    row.set_result(QuotaResult(
        plan_id="p1", ok=False, error="fetch_failed", message="网络异常",
        fetched_at=int(time.time()),
    ))
    assert current_theme().fg_dim in row.window_label.styleSheet()
    assert current_theme().fg_stale not in row.window_label.styleSheet()


def test_elide_applies_before_first_layout(qapp) -> None:
    """首帧布局前 label.width()≈0：省略必须按固定宽度生效（以 … 结尾）。"""
    row = PlanRow(_plan())
    long_text = "这是一个很长很长的窗口标签文本" * 5
    row.set_result(QuotaResult(
        plan_id="p1", ok=False, error="fetch_failed", message=long_text,
        fetched_at=int(time.time()),
    ))
    text = row.window_label.text()
    assert text.endswith("…")
    assert len(text) < len(long_text)


def test_tick_refreshes_expanded_detail(qapp, monkeypatch) -> None:
    """展开详情的倒计时文本随 tick 滚动，不冻结到下次 set_result。"""
    row = PlanRow(_plan())
    fetched = int(time.time())
    row.set_result(QuotaResult(
        plan_id="p1", ok=True, fetched_at=fetched,
        windows=[
            QuotaWindow(key="5h", label="5小时", used_percent=50.0, reset_in_sec=3600),
            QuotaWindow(key="7d", label="7天", used_percent=20.0, reset_in_sec=86400),
        ],
    ))
    row._expanded = True
    before = row.detail_label.text()
    assert before  # 多窗口时详情非空
    monkeypatch.setattr(time, "time", lambda: fetched + 600)
    row.tick()
    assert row.detail_label.text() != before


# ------------------------------------------------------------ 应用装配

def test_pause_state_restored_from_config(qapp, sample_db, tmp_path, monkeypatch) -> None:
    """refresh_enabled=false 持久化后，重启回读到托盘暂停勾选（不被 _tick 清除）。"""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"refresh": {"globalEnabled": False}}), encoding="utf-8"
    )
    monkeypatch.setenv("TOKEN_WIDGET_CONFIG", str(cfg_file))
    ctx = AppContext(store=Store(sample_db))
    app = Application(ctx)
    try:
        assert app.tray.action_pause.isChecked()
        assert ctx.config.refresh_enabled is False
        app._tick()  # _tick 从托盘勾选态取暂停状态，不得静默清除
        assert ctx.config.refresh_enabled is False
        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["refresh"]["globalEnabled"] is False
    finally:
        app.tick_timer.stop()
        app.db_timer.stop()


def test_pin_state_restored_to_tray(qapp, sample_db, tmp_path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"window": {"alwaysOnTop": True}}), encoding="utf-8"
    )
    monkeypatch.setenv("TOKEN_WIDGET_CONFIG", str(cfg_file))
    ctx = AppContext(store=Store(sample_db))
    app = Application(ctx)
    try:
        assert app.tray.action_pin.isChecked() is True
        # 托盘 -> 窗口单向同步（信号已接线）
        app.tray.action_pin.setChecked(False)
        assert ctx.config.always_on_top is False
        assert app.window.btn_pin.isChecked() is False
        # 窗口 -> 托盘回同步
        app.window.btn_pin.click()
        assert ctx.config.always_on_top is True
        assert app.tray.action_pin.isChecked() is True
    finally:
        app.tick_timer.stop()
        app.db_timer.stop()


def test_identical_cookie_skip_save(qapp, sample_db, tmp_path, monkeypatch) -> None:
    """捕获的 auth_cookie 与已存值相同：跳过保存与刷新（防重存死循环）。"""
    from token_widget.config import OpencodeGoAccount

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TOKEN_WIDGET_CONFIG", str(cfg_file))
    ctx = AppContext(store=Store(sample_db))
    app = Application(ctx)
    try:
        same = OpencodeGoAccount(auth_cookie="cookie-x", workspace_id="wrk_1")
        ctx.config.opencode_go_accounts["main"] = same
        mtime_before = cfg_file.stat().st_mtime_ns
        refreshed: list[bool] = []
        monkeypatch.setattr(app, "_refresh_all", lambda: refreshed.append(True))
        app._save_opencode_account(
            OpencodeGoAccount(auth_cookie="cookie-x", workspace_id="wrk_1")
        )
        assert refreshed == []
        assert cfg_file.stat().st_mtime_ns == mtime_before  # 未写盘
        # 不同 cookie 正常保存并触发刷新
        app._save_opencode_account(
            OpencodeGoAccount(auth_cookie="cookie-y", workspace_id="wrk_1")
        )
        assert refreshed == [True]
        assert ctx.config.opencode_go_accounts["main"].auth_cookie == "cookie-y"
    finally:
        app.tick_timer.stop()
        app.db_timer.stop()


# ------------------------------------------------------------ 设置对话框

def test_settings_dialog_emits_theme(qapp) -> None:
    """主题单选：勾选即发出 theme_selected；selected_theme 返回当前项。"""
    from token_widget.ui.settings import SettingsDialog

    dialog = SettingsDialog("dark")
    emitted: list[str] = []
    dialog.theme_selected.connect(emitted.append)
    dialog._radios["light"].setChecked(True)
    assert emitted == ["light"]
    assert dialog.selected_theme() == "light"
    dialog._radios["light"].setChecked(True)  # 同值不重复发
    assert emitted == ["light"]


# ------------------------------------------------------------ 主题系统

def test_all_themes_complete_and_switchable(qapp) -> None:
    """每套主题字段齐备且可切换；未知 key 回退 dark。"""
    from token_widget.ui import theme as theme_mod

    for key, th in theme_mod.THEMES.items():
        assert th.key == key and th.label
        assert th.bg_rgba and th.bg_solid and th.fg_main and th.fg_dim
        assert set(th.percent) == {"green", "orange", "red"}
        assert theme_mod.set_current(key) is th
        assert theme_mod.current().key == key
    theme_mod.set_current("dark")
    assert theme_mod.set_current("nonexistent").key == "dark"


def test_settings_dialog_lists_all_themes(qapp) -> None:
    """设置对话框与托盘子菜单的主题项 = THEMES 全集（新增主题自动出现）。"""
    from token_widget.ui import theme as theme_mod
    from token_widget.ui.settings import SettingsDialog

    dialog = SettingsDialog("dark")
    assert set(dialog._radios) == set(theme_mod.THEMES)
    tray = TrayIcon()
    assert {a.text() for a in tray.theme_menu.actions()} == {
        th.label for th in theme_mod.THEMES.values()
    }

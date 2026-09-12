"""应用装配：上下文、定时器、信号接线（§5 调度驱动 + §3.1 DB 变更跟随）。"""

from __future__ import annotations

import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from ..collectors import is_opencode_go_native
from ..config import save as save_config
from ..models import ERROR_TEXTS, QuotaResult, percent_color_class
from . import theme as theme_mod
from .context import AppContext
from .main_window import MainWindow
from .picker import OpencodeCredentialsDialog, PickerDialog
from .tray import TrayIcon

TICK_MS = 1000       # 调度器心跳 + 倒计时刷新
DB_WATCH_MS = 2000   # §3.1 mtime 轮询间隔
RELOGIN_COOLDOWN_S = 300.0  # cookie_expired 自动弹重登的冷却时间（防刷屏）


class Application:
    def __init__(self, ctx: AppContext | None = None) -> None:
        self.ctx = ctx or AppContext()
        theme_mod.set_current(self.ctx.config.theme)  # 建窗前先生效持久化主题
        # 样式只挂主窗与自有对话框：托盘菜单保持系统原生，不随主题换肤
        self.window = MainWindow(self.ctx.config)
        self.tray = TrayIcon(self.ctx.config.theme)
        self._last_relogin_prompt_at = 0.0
        self._relogin_dialog = None  # 在屏的非模态重登对话框（防堆叠）
        self._consumption_date = time.strftime("%Y-%m-%d")  # 今日消耗所属日期
        self._wire()
        # 回读持久化状态：暂停（toggled 幂等重写 config，可接受）与置顶勾选态
        self.tray.action_pause.setChecked(not self.ctx.config.refresh_enabled)
        self.tray.action_pin.setChecked(self.ctx.config.always_on_top)

        self.ctx.reload_plans()  # plans_changed 信号已触发行列表重建
        self.ctx.refresh_consumption()
        if not any(p.selected for p in self.ctx.plans):
            QTimer.singleShot(500, self.open_picker)  # 首次启动引导勾选

        self.tick_timer = QTimer()
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(TICK_MS)

        self.db_timer = QTimer()
        self.db_timer.timeout.connect(self._watch_db)
        self.db_timer.start(DB_WATCH_MS)

    # ------------------------------------------------------------ 接线

    def _wire(self) -> None:
        ctx = self.ctx
        ctx.signals.quota_updated.connect(self._on_quota_updated)
        ctx.signals.plans_changed.connect(self._on_plans_changed)
        ctx.signals.consumption_updated.connect(self.window.set_consumption)

        self.window.picker_requested.connect(self.open_picker)
        self.window.refresh_all_requested.connect(self._refresh_all)
        self.window.settings_requested.connect(self.open_settings)
        self.tray.refresh_all_requested.connect(self._refresh_all)
        self.tray.picker_requested.connect(self.open_picker)
        self.tray.pause_toggled.connect(self._on_pause_toggled)
        self.tray.pin_toggled.connect(self.window.set_always_on_top)
        self.window.pin_changed.connect(self.tray.action_pin.setChecked)
        self.tray.theme_selected.connect(self._on_theme_selected)
        self.tray.quit_requested.connect(self.quit)
        self.tray.clicked.connect(self._toggle_window)

    # ------------------------------------------------------------ 槽

    def _tick(self) -> None:
        self.ctx.scheduler.set_paused(self.tray.action_pause.isChecked())
        self.ctx.dispatch_due()
        self.window.plan_list.tick()
        today = time.strftime("%Y-%m-%d")
        if today != self._consumption_date:
            self._consumption_date = today
            self.ctx.refresh_consumption(quick=True)  # 跨午夜：今日消耗滚动

    def _watch_db(self) -> None:
        if not self.ctx.db_changed():
            return
        # quick 模式：CC Switch 写库持锁时短超时快速失败（不重试），
        # 下个轮询周期再试，避免在 UI 线程上卡数秒
        self.ctx.store.probe(quick=True)
        self.ctx.reload_plans(quick=True)
        self.ctx.refresh_consumption(quick=True)

    def _on_quota_updated(self, plan_id: str, result: QuotaResult) -> None:
        row = self.window.plan_list.rows.get(plan_id)
        if row is not None:
            row.set_result(result)
        # 展示的可能是 stale 回退；真实失败结果在调度器里
        real = self.ctx.scheduler.last_result(plan_id)
        if real is not None and real.error == "cookie_expired":
            self._maybe_prompt_relogin(plan_id)
        self._update_tray_color()

    def _on_plans_changed(self) -> None:
        selected = [p for p in self.ctx.plans if p.selected]
        self.window.rebuild_rows(selected)
        for plan in selected:
            row = self.window.plan_list.rows.get(plan.id)
            if row is not None:
                display = self.ctx.display_result(plan.id)
                if display is not None:
                    row.set_result(display)
        self._update_tray_color()

    def _on_pause_toggled(self, paused: bool) -> None:
        self.ctx.scheduler.set_paused(paused)
        self.ctx.config.refresh_enabled = not paused
        save_config(self.ctx.config)

    def _on_theme_selected(self, key: str) -> None:
        """主题切换：持久化 + 主窗/行重建即时套用（托盘菜单保持系统原生）。"""
        theme_mod.set_current(key)
        self.ctx.config.theme = key
        save_config(self.ctx.config)
        self.tray.set_theme(key)
        self.tray.set_status_color(None)  # 待机色随主题更新（下次配额刷新会再覆盖）
        self.window.apply_theme()
        self._on_plans_changed()  # 重建行套用新配色

    def _refresh_all(self) -> None:
        self.ctx.scheduler.request_refresh_all()
        self.ctx.dispatch_due()

    def _toggle_window(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        else:
            self.window.show()
            self.window.raise_()

    def _update_tray_color(self) -> None:
        worst_pct = None
        for plan_id in self.window.plan_list.rows:
            # 与行显示一致：用含 stale 回退的展示结果（§6.3 图标 = 行内最紧张窗口颜色）
            result = self.ctx.display_result(plan_id)
            if result and result.ok:
                worst = result.worst
                if worst is not None and worst.used_percent is not None:
                    worst_pct = (
                        worst.used_percent
                        if worst_pct is None
                        else max(worst_pct, worst.used_percent)
                    )
        color = (
            theme_mod.percent_colors()[percent_color_class(worst_pct)]
            if worst_pct is not None
            else None
        )
        self.tray.set_status_color(color)

    # ------------------------------------------------------------ 面板

    def open_picker(self) -> None:
        picker = PickerDialog(self.ctx.plans, self.ctx.config, self.window)
        picker.setAttribute(Qt.WA_DeleteOnClose)  # exec 型对话框：关闭即释放 C++ 对象
        picker.opencode_setup_requested.connect(self.open_opencode_credentials)
        picker.exec()
        # 勾选状态以 config 为准，重新同步；quick 模式避免 DB 锁定时卡 UI 线程
        self.ctx.reload_plans(quick=True)

    def open_settings(self) -> None:
        """设置对话框：主题选中即实时预览（与托盘子菜单同链路）。"""
        from .settings import SettingsDialog

        dialog = SettingsDialog(self.ctx.config.theme, self.window)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.theme_selected.connect(self._on_theme_selected)
        dialog.exec()

    def open_opencode_credentials(self, modal: bool = True) -> None:
        """M3 一键重登：内嵌 webview 自动捕获 auth cookie + workspace_id。

        QtWebEngine 不可用时回退手动粘贴对话框。
        """
        cfg = self.ctx.config
        try:
            from .webview_login import WebviewLoginDialog
        except ImportError:
            self._open_manual_credentials(modal=modal)
            return

        dialog = WebviewLoginDialog(cfg.default_opencode_account_data, self.window)
        dialog.setAttribute(Qt.WA_DeleteOnClose)  # webview+profile 很重，关闭即释放
        if modal:
            if dialog.exec():
                # deferred delete 在事件循环返回前不执行，此处读 account 仍安全
                self._apply_opencode_account(dialog)
            return
        self._relogin_dialog = dialog
        dialog.open()
        dialog.finished.connect(lambda _res: self._on_relogin_finished(dialog))

    def _on_relogin_finished(self, dialog) -> None:
        if self._relogin_dialog is dialog:
            self._relogin_dialog = None
        self._apply_opencode_account(dialog)

    def _open_manual_credentials(self, modal: bool = True) -> None:
        cfg = self.ctx.config
        dialog = OpencodeCredentialsDialog(cfg.default_opencode_account_data, self.window)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        if modal:
            if dialog.exec() and dialog.account.ready:
                self._save_opencode_account(dialog.account)
            return
        # §4.4 非模态：自动重登不阻塞 UI
        self._relogin_dialog = dialog
        dialog.open()
        dialog.finished.connect(lambda res: self._on_manual_finished(dialog, res))

    def _on_manual_finished(self, dialog, res: int) -> None:
        if self._relogin_dialog is dialog:
            self._relogin_dialog = None
        if res and dialog.account.ready:
            self._save_opencode_account(dialog.account)

    def _apply_opencode_account(self, dialog) -> None:
        if dialog.account.ready:
            self._save_opencode_account(dialog.account)

    def _save_opencode_account(self, account) -> None:
        cfg = self.ctx.config
        existing = cfg.default_opencode_account_data
        if existing is not None and existing.auth_cookie == account.auth_cookie:
            # 捕获到的是已存旧 cookie（本地未过期但服务端已失效）：
            # 重存无效且会触发「保存→仍失效→再弹」死循环，跳过保存与刷新
            return
        cfg.opencode_go_accounts[cfg.default_opencode_account] = account
        save_config(cfg)
        self._refresh_all()  # 新凭据立即重试采集

    def _maybe_prompt_relogin(self, plan_id: str) -> None:
        """cookie_expired 自动弹出重登（非模态）；冷却 5 分钟防刷屏。"""
        if self._relogin_dialog is not None and self._relogin_dialog.isVisible():
            return  # 已有重登对话框在屏，防堆叠（且共享同名持久化 profile）
        now = time.monotonic()
        if now - self._last_relogin_prompt_at < RELOGIN_COOLDOWN_S:
            return
        plan = next((p for p in self.ctx.plans if p.id == plan_id), None)
        # 采集在途期间用户可能已取消勾选，此时不再打扰
        if plan is None or not plan.selected or not is_opencode_go_native(plan):
            return
        self._last_relogin_prompt_at = now
        self.open_opencode_credentials(modal=False)

    # ------------------------------------------------------------ 生命周期

    def quit(self) -> None:
        self.tick_timer.stop()
        self.db_timer.stop()
        self.ctx.begin_shutdown()  # 在途 Worker 不再广播（防已删除信号源竞态）
        self.window.save_geometry()
        save_config(self.ctx.config)
        QApplication.quit()

    def show(self) -> None:
        self.window.show()
        self.tray.show()


def run(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # QtWebEngine 要求：必须在 QApplication 创建前设置共享 OpenGL 上下文
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    from .icon import app_icon

    _icon = app_icon()
    if _icon is not None:
        # 主窗无边框不显示；作用于任务栏/alt-tab 与各自有对话框
        app.setWindowIcon(_icon)
    application = Application()
    application.show()
    if "--smoke" in argv:
        QTimer.singleShot(2000, application.quit)
    if "--smoke-webview" in argv:
        # 打包冒烟：验证 QtWebEngine 在 frozen 环境下能初始化
        from .webview_login import WebviewLoginDialog

        QTimer.singleShot(500, lambda: WebviewLoginDialog(None).show())
        QTimer.singleShot(6000, application.quit)
    if "--opencode-login" in argv:
        # 启动即弹出 OpenCode Go 内嵌登录（自动捕获 auth cookie + workspace_id）
        QTimer.singleShot(600, lambda: application.open_opencode_credentials(modal=False))
    return app.exec()

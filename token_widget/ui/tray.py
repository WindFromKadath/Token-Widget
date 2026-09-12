"""托盘（§6.3）：菜单 + 主题切换子菜单 + 产品图标（状态色以右下角徽标呈现）。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import theme as theme_mod
from .icon import app_icon
from .theme import THEMES


def dot_icon(color_hex: str) -> QIcon:
    """16x16 圆点图标（产品图标缺失时的回退）。"""
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color_hex))
    painter.setPen(QColor(0, 0, 0, 60))
    painter.drawEllipse(2, 2, 12, 12)
    painter.end()
    return QIcon(pixmap)


def badged_icon(base: QIcon, color_hex: str) -> QIcon:
    """产品图标右下角叠状态色圆点徽标（最紧张窗口颜色，多尺寸）。"""
    icon = QIcon()
    for size in (16, 24, 32, 48):
        pix = base.pixmap(size, size)
        if pix.isNull():
            continue
        d = max(6, round(size * 0.38))
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(color_hex))
        painter.setPen(QColor(0, 0, 0, 90))
        painter.drawEllipse(size - d - 1, size - d - 1, d, d)
        painter.end()
        icon.addPixmap(pix)
    return icon


class TrayIcon(QSystemTrayIcon):
    refresh_all_requested = Signal()
    picker_requested = Signal()
    theme_selected = Signal(str)
    pause_toggled = Signal(bool)
    pin_toggled = Signal(bool)
    quit_requested = Signal()
    clicked = Signal()

    def __init__(self, current_theme: str = "dark", parent=None) -> None:
        product = app_icon()  # 产品图标；缺失时回退旧圆点行为
        base = product or dot_icon(theme_mod.current().fg_dim)
        super().__init__(base, parent)
        self._product_icon = product
        self.setToolTip("Token 挂件")

        menu = QMenu()
        self.action_refresh = QAction("刷新全部", menu)
        self.action_picker = QAction("选择套餐…", menu)
        self.action_pause = QAction("暂停自动刷新", menu)
        self.action_pause.setCheckable(True)
        self.action_pin = QAction("置顶", menu)
        self.action_pin.setCheckable(True)
        self.action_quit = QAction("退出", menu)
        menu.addAction(self.action_refresh)
        menu.addAction(self.action_picker)

        # 主题子菜单（单选）
        self.theme_menu = QMenu("主题", menu)
        group = QActionGroup(self)
        group.setExclusive(True)
        self._theme_actions: dict[str, QAction] = {}
        for key, th in THEMES.items():
            act = QAction(th.label, self.theme_menu)
            act.setCheckable(True)
            group.addAction(act)
            self.theme_menu.addAction(act)
            self._theme_actions[key] = act
            act.triggered.connect(lambda _checked=False, k=key: self.theme_selected.emit(k))
        menu.addMenu(self.theme_menu)

        menu.addAction(self.action_pause)
        menu.addAction(self.action_pin)
        menu.addSeparator()
        menu.addAction(self.action_quit)
        self.setContextMenu(menu)

        self.action_refresh.triggered.connect(self.refresh_all_requested.emit)
        self.action_picker.triggered.connect(self.picker_requested.emit)
        self.action_pause.toggled.connect(self.pause_toggled.emit)
        self.action_pin.toggled.connect(self.pin_toggled.emit)
        self.action_quit.triggered.connect(self.quit_requested.emit)
        self.activated.connect(self._on_activated)
        self.set_theme(current_theme)

    def set_theme(self, key: str) -> None:
        """同步主题单选勾选状态。"""
        act = self._theme_actions.get(key)
        if act is not None:
            act.setChecked(True)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.clicked.emit()

    def set_status_color(self, color_hex: str | None) -> None:
        """状态色：产品图标右下角徽标；None 显示无徽标原图（产品图标缺失回退圆点）。"""
        if self._product_icon is None:
            self.setIcon(dot_icon(color_hex or theme_mod.current().fg_dim))
            return
        if color_hex is None:
            self.setIcon(self._product_icon)
            return
        self.setIcon(badged_icon(self._product_icon, color_hex))

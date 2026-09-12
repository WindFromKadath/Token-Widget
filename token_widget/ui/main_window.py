"""主窗（§6.1）：无边框、圆角、主题化（深/浅/磨砂玻璃）、置顶可切、拖拽移动、尺寸可调。

Esc/点外不关闭；关闭按钮收进托盘。尺寸用右下角 QSizeGrip 调整并持久化到 config。
磨砂玻璃主题尝试 DWM 亚克力（_set_acrylic），失败自动退回高不透明底色。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from ..config import WidgetConfig
from ..models import ConsumptionSummary
from . import theme as theme_mod
from .icon import glyph_pixmap
from .plan_row import PlanRow
from .theme import MIN_WINDOW_WIDTH, ROW_HEIGHT, WINDOW_WIDTH, qss_base, rgba_tuple

MAX_CONTENT_HEIGHT = 640
_CHROME_HEIGHT = 108  # 标题栏 + 底部消耗栏 + 边距的估算高度


class PlanListWidget(QWidget):
    """行容器：按勾选清单重建行。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._layout.addStretch(1)
        self.rows: dict[str, PlanRow] = {}
        self._hint: QLabel | None = None

    def rebuild(self, plans) -> None:
        for row in self.rows.values():
            self._layout.removeWidget(row)
            row.deleteLater()
        self.rows.clear()
        if self._hint is not None:
            self._layout.removeWidget(self._hint)
            self._hint.deleteLater()
            self._hint = None
        if not plans:
            self._hint = QLabel("还没有展示的套餐\n点左上角菜单或托盘图标选择")
            self._hint.setAlignment(Qt.AlignCenter)
            self._hint.setStyleSheet(
                f"color: {theme_mod.current().fg_dim}; padding: 16px 0;"
            )
            self._layout.insertWidget(self._layout.count() - 1, self._hint)
            return
        for plan in plans:
            row = PlanRow(plan)
            self.rows[plan.id] = row
            self._layout.insertWidget(self._layout.count() - 1, row)

    def set_consumption(self, summary: ConsumptionSummary | None) -> None:
        """把当日消耗分发到各行的详情面板。"""
        by_plan = {e.plan_id: e for e in summary.by_plan} if summary else {}
        for plan_id, row in self.rows.items():
            row.set_consumption(by_plan.get(plan_id))

    def tick(self) -> None:
        for row in self.rows.values():
            row.tick()


class MainWindow(QWidget):
    picker_requested = Signal()
    refresh_all_requested = Signal()
    settings_requested = Signal()
    hidden_to_tray = Signal()
    pin_changed = Signal(bool)  # 标题栏图钉 -> 托盘置顶勾选同步

    def __init__(self, config: WidgetConfig) -> None:
        super().__init__()
        self._config = config
        self._drag_offset: QPoint | None = None
        self._last_summary: ConsumptionSummary | None = None
        self._backdrop_ok = False
        self.setWindowTitle("Token 挂件")
        self.setStyleSheet(qss_base())
        self.setMinimumSize(MIN_WINDOW_WIDTH, 160)
        self._apply_flags()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 10, 0, 8)
        container_layout.setSpacing(6)
        outer.addWidget(container)

        # 标题栏（矢量图标按钮）
        bar = QHBoxLayout()
        bar.setContentsMargins(12, 0, 8, 0)
        title = QLabel("Token 挂件")
        title.setStyleSheet("font-weight: 600; font-size: 13px;")
        self.btn_picker = QPushButton()
        self.btn_picker.setToolTip("选择套餐")
        self.btn_refresh = QPushButton()
        self.btn_refresh.setToolTip("刷新全部")
        self.btn_settings = QPushButton()
        self.btn_settings.setToolTip("设置")
        self.btn_pin = QPushButton()
        self.btn_pin.setCheckable(True)
        self.btn_pin.setChecked(config.always_on_top)
        self.btn_pin.setToolTip("置顶")
        self.btn_close = QPushButton()
        self.btn_close.setToolTip("收进托盘")
        for btn in (
            self.btn_picker, self.btn_refresh, self.btn_settings,
            self.btn_pin, self.btn_close,
        ):
            btn.setFixedSize(24, 24)
        bar.addWidget(title)
        bar.addStretch(1)
        bar.addWidget(self.btn_picker)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_settings)
        bar.addWidget(self.btn_pin)
        bar.addWidget(self.btn_close)
        container_layout.addLayout(bar)

        # Plan 行
        self.plan_list = PlanListWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.plan_list)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 视口透明，露出主窗自绘的圆角底
        scroll.setStyleSheet("background: transparent; border: none;")
        scroll.viewport().setStyleSheet("background: transparent;")
        container_layout.addWidget(scroll, 1)

        # 底部：当日消耗 + 尺寸调整柄
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        self.consume_label = QLabel("今日 $0.00 · 0 tokens")
        bottom.addWidget(self.consume_label, 1)
        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(14, 14)
        bottom.addWidget(self._grip, 0, Qt.AlignRight | Qt.AlignBottom)
        container_layout.addLayout(bottom)

        self.btn_picker.clicked.connect(self.picker_requested.emit)
        self.btn_refresh.clicked.connect(self.refresh_all_requested.emit)
        self.btn_settings.clicked.connect(self.settings_requested.emit)
        self.btn_pin.clicked.connect(self._toggle_top)
        self.btn_close.clicked.connect(self._hide_to_tray)

        self._resize_save_timer = QTimer(self)
        self._resize_save_timer.setSingleShot(True)
        self._resize_save_timer.setInterval(400)
        self._resize_save_timer.timeout.connect(self.save_geometry)

        self._style_chrome()
        self._restore_geometry()
        self._resize_to_rows()

    # ------------------------------------------------------------ 外观

    def _apply_flags(self) -> None:
        flags = Qt.FramelessWindowHint | Qt.Window | Qt.Tool
        if self._config.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def _style_chrome(self) -> None:
        """随主题变化的局部样式（底栏分隔线、按钮图标）。"""
        t = theme_mod.current()
        self.consume_label.setStyleSheet(
            f"color: {t.fg_dim}; padding: 6px 12px 2px 12px;"
            f" border-top: 1px solid {t.border};"
        )
        dim, main = t.fg_dim, t.fg_main
        for btn, name, color in (
            (self.btn_picker, "menu", dim),
            (self.btn_refresh, "refresh", dim),
            (self.btn_settings, "sliders", dim),
            (self.btn_pin, "pin", main if self.btn_pin.isChecked() else dim),
            (self.btn_close, "close", dim),
        ):
            pix = glyph_pixmap(name, color, 14)
            if pix is not None:
                btn.setText("")
                btn.setIcon(QIcon(pix))
            else:  # 矢量渲染不可用时的文本兜底
                btn.setText(
                    {"menu": "☰", "refresh": "⟳", "sliders": "⚙",
                     "pin": "📌", "close": "✕"}[name]
                )

    def apply_theme(self) -> None:
        """主题切换：重套样式、重绘图标、开/关磨砂、触发重绘。"""
        self.setStyleSheet(qss_base())
        self._style_chrome()
        self._apply_backdrop()
        self.update()

    def _apply_backdrop(self) -> None:
        t = theme_mod.current()
        self._backdrop_ok = _set_acrylic(int(self.winId()), t.acrylic)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_backdrop()  # 句柄就绪后再试一次（首次构造时可能过早）

    def paintEvent(self, event) -> None:  # noqa: N802
        t = theme_mod.current()
        bg = t.bg_rgba
        if t.acrylic and not self._backdrop_ok:
            bg = t.bg_rgba_fallback or t.bg_rgba
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)
        painter.fillPath(path, QBrush(QColor(*rgba_tuple(bg))))
        painter.setPen(QColor(t.border))
        painter.drawPath(path)

    def _resize_to_rows(self) -> None:
        """内容行数决定推荐高度；仅在用户未手动调整过时自动套用。"""
        rows = max(len(self.plan_list.rows), 1)
        need = min(_CHROME_HEIGHT + ROW_HEIGHT * rows, MAX_CONTENT_HEIGHT)
        if self._config.window_h is None:
            self.resize(self._config.window_w or WINDOW_WIDTH, need)

    def rebuild_rows(self, plans) -> None:
        self.plan_list.rebuild(plans)
        self.plan_list.set_consumption(self._last_summary)
        self._resize_to_rows()

    # ------------------------------------------------------------ 行为

    def set_consumption(self, summary: ConsumptionSummary | None) -> None:
        self._last_summary = summary
        if summary is None or (summary.total_cost_usd == 0 and summary.total_tokens == 0):
            self.consume_label.setText("今日 $0.00 · 0 tokens")
        else:
            self.consume_label.setText(
                f"今日 ${summary.total_cost_usd:.2f} · {summary.total_tokens:,} tokens"
            )
        self.plan_list.set_consumption(summary)

    def _toggle_top(self) -> None:
        self.set_always_on_top(self.btn_pin.isChecked())

    def set_always_on_top(self, on_top: bool) -> None:
        """置顶开关（标题栏图钉与托盘菜单共用入口，双向同步）。

        幂等：同值调用不发信号、不重设 flags（防信号回环）。
        """
        if on_top == self._config.always_on_top and self.btn_pin.isChecked() == on_top:
            return
        self._config.always_on_top = on_top
        self.btn_pin.setChecked(on_top)
        self._apply_flags()
        self._style_chrome()  # 置顶按钮高亮态随 checked 变化
        self.show()  # 重设 flags 后需重新 show
        self.pin_changed.emit(on_top)  # 同步托盘勾选

    def _hide_to_tray(self) -> None:
        self.save_geometry()
        self.hide()
        self.hidden_to_tray.emit()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Esc/点外不关闭；系统关闭请求也先收托盘
        event.ignore()
        self._hide_to_tray()

    # ------------------------------------------------------------ 拖拽与缩放

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        self.save_geometry()
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_resize_save_timer"):
            self._resize_save_timer.start()  # 拖动连续触发，去抖后落盘

    # ------------------------------------------------------------ 持久化

    def _restore_geometry(self) -> None:
        if self._config.window_w and self._config.window_h:
            self.resize(self._config.window_w, self._config.window_h)
        if self._config.window_x is not None and self._config.window_y is not None:
            self.move(self._config.window_x, self._config.window_y)
            self._clamp_onscreen()

    def _clamp_onscreen(self) -> None:
        """坐标落在已断开的副屏时拉回主屏可用区域（无边框窗无法拖回）。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        max_x = max(geo.left(), geo.right() - self.width() + 1)
        max_y = max(geo.top(), geo.bottom() - self.height() + 1)
        x = min(max(self.x(), geo.left()), max_x)
        y = min(max(self.y(), geo.top()), max_y)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def save_geometry(self) -> None:
        """把当前位置与尺寸写入 config（供退出/收托盘时调用）。"""
        self._config.window_x = self.x()
        self._config.window_y = self.y()
        if self.width() > 0 and self.height() > 0:
            self._config.window_w = self.width()
            self._config.window_h = self.height()


# ------------------------------------------------------------ DWM 亚克力

def _set_acrylic(hwnd: int, enable: bool) -> bool:
    """Windows 亚克力磨砂背景开关；返回磨砂是否实际生效（非 Windows 恒 False）。

    优先 DWM system backdrop + 圆角偏好（Win11 22H2+）：模糊层随窗口裁圆角。
    旧系统退回 SetWindowCompositionAttribute accent——模糊层只能是矩形，
    四角会露出直角（Win10 平台限制，无 API 可裁）。
    """
    import sys

    if sys.platform != "win32":
        return False
    try:
        import ctypes

        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWA_SYSTEMBACKDROP_TYPE = 38
        DWM_WINDOW_CORNER_ROUND = 2
        DWMSBT_DISABLE = 1
        DWMSBT_TRANSIENTWINDOW = 3  # 亚克力

        dwm = ctypes.windll.dwmapi
        corner = ctypes.c_int(DWM_WINDOW_CORNER_ROUND if enable else 0)
        dwm.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner), 4
        )
        backdrop = ctypes.c_int(DWMSBT_TRANSIENTWINDOW if enable else DWMSBT_DISABLE)
        hr = dwm.DwmSetWindowAttribute(
            hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(backdrop), 4
        )
        if enable and hr == 0:
            _set_accent(hwnd, False)
            return True
        return _set_accent(hwnd, enable)
    except Exception:
        return False


def _set_accent(hwnd: int, enable: bool) -> bool:
    """旧版 accent 磨砂（矩形模糊层，四角为直角）；返回磨砂是否生效。"""
    try:
        import ctypes
        from ctypes import wintypes

        WCA_ACCENT_POLICY = 19
        ACCENT_DISABLED = 0
        ACCENT_ENABLE_ACRYLICBLURBEHIND = 4

        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", wintypes.DWORD),
                ("AccentFlags", wintypes.DWORD),
                ("GradientColor", wintypes.DWORD),
                ("AnimationId", wintypes.DWORD),
            ]

        class WCA_DATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", wintypes.DWORD),
                ("Data", ctypes.POINTER(ACCENT_POLICY)),
                ("SizeOfData", ctypes.c_size_t),
            ]

        state = ACCENT_ENABLE_ACRYLICBLURBEHIND if enable else ACCENT_DISABLED
        accent = ACCENT_POLICY(state, 0, 0, 0)
        data = WCA_DATA(WCA_ACCENT_POLICY, ctypes.pointer(accent), ctypes.sizeof(accent))
        ok = bool(
            ctypes.windll.user32.SetWindowCompositionAttribute(
                int(hwnd), ctypes.byref(data)
            )
        )
        return ok and enable
    except Exception:
        return False

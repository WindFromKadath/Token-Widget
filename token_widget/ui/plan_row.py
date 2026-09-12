"""Plan 行组件（§6.1：图标/名称/额度条/百分比/重置倒计时/状态）。

多窗口 plan 行内显示最紧张窗口，点击行展开/收起全部窗口。
颜色经 theme.current() 取，主题切换后重建行即可生效；状态角标用矢量 glyph。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..models import (
    ERROR_TEXTS,
    ConsumptionByPlan,
    Plan,
    QuotaResult,
    fmt_duration_sec,
)
from . import theme as theme_mod
from .icon import glyph_pixmap, icon_pixmap
from .theme import ROW_HEIGHT


class QuotaBar(QProgressBar):
    """6px 高的细额度条。"""

    def __init__(self) -> None:
        super().__init__()
        self.setRange(0, 100)
        self.setTextVisible(False)
        self.setFixedHeight(6)
        self._color = theme_mod.percent_colors()["green"]

    def set_percent(self, percent: float) -> None:
        self.setValue(int(max(0.0, min(percent, 100.0))))
        colors = theme_mod.percent_colors()
        if percent >= 90:
            self._color = colors["red"]
        elif percent >= 70:
            self._color = colors["orange"]
        else:
            self._color = colors["green"]
        self.setStyleSheet(
            f"QProgressBar {{ border: none; background: {theme_mod.current().bar_bg};"
            f" border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {self._color};"
            f" border-radius: 3px; }}"
        )


def _elide(label: QLabel, text: str) -> None:
    """按 label 当前实际宽度省略文本（QLabel 默认裁剪无省略号，难看）。

    首帧布局前 width≈0：用窗口标签列的典型宽度兜底，保证省略仍然生效。
    """
    width = label.width()
    if width <= 8:
        width = 112
    label.setText(label.fontMetrics().elidedText(text, Qt.ElideRight, width))


class PlanRow(QFrame):
    clicked = Signal(str)  # plan_id

    def __init__(self, plan: Plan, parent=None) -> None:
        super().__init__(parent)
        self.plan = plan
        self._result: QuotaResult | None = None
        self._consumption: ConsumptionByPlan | None = None
        self._expanded = False
        self.setObjectName("planRow")  # 主题里的行 hover 高亮
        self.setFixedHeight(ROW_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self._build()
        self._render_loading()

    # ------------------------------------------------------------ 构建

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 4, 10, 4)
        outer.setSpacing(0)

        main = QHBoxLayout()
        main.setSpacing(6)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(22, 22)
        self.icon_label.setAlignment(Qt.AlignCenter)
        icon = icon_pixmap(self.plan.icon, 16)
        if icon is not None:
            self.icon_label.setPixmap(icon)
            self.icon_label.setStyleSheet("background: transparent;")
        else:
            self.icon_label.setText(self._initial())
            self.icon_label.setStyleSheet(
                f"background: {self.plan.icon_color or '#30363d'}; color: #ffffff;"
                "border-radius: 11px; font-weight: 600; font-size: 11px;"
            )

        self.name_label = QLabel()
        self.name_label.setFixedWidth(92)
        self._set_name()

        mid = QVBoxLayout()
        mid.setSpacing(2)
        self.window_label = QLabel("")
        self.window_label.setStyleSheet(
            f"color: {theme_mod.current().fg_dim}; font-size: 10px;"
        )
        self.window_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.window_label.setMinimumWidth(60)
        self.bar = QuotaBar()
        mid.addWidget(self.window_label)
        mid.addWidget(self.bar)

        self.percent_label = QLabel("…")
        self.percent_label.setFixedWidth(38)
        self.percent_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.reset_label = QLabel("")
        self.reset_label.setFixedWidth(50)
        self.reset_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.reset_label.setStyleSheet(
            f"color: {theme_mod.current().fg_dim}; font-size: 10px;"
        )

        self.status_label = QLabel("")
        self.status_label.setFixedWidth(14)
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        main.addWidget(self.icon_label)
        main.addWidget(self.name_label)
        main.addLayout(mid, 1)
        main.addWidget(self.percent_label)
        main.addWidget(self.reset_label)
        main.addWidget(self.status_label)
        outer.addLayout(main)

        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet(
            f"color: {theme_mod.current().fg_dim}; font-size: 10px;"
            " padding: 2px 0 0 34px;"
        )
        self.detail_label.setVisible(False)
        self.detail_label.setWordWrap(True)
        outer.addWidget(self.detail_label)

    def _initial(self) -> str:
        name = self.plan.name.strip()
        return (name[0] if name else "?").upper()

    def _display_name(self) -> str:
        suffix = " ●" if self.plan.is_current else ""
        return f"{self.plan.name}{suffix}"

    def _set_name(self) -> None:
        self.name_label.setText(
            self.name_label.fontMetrics().elidedText(
                self._display_name(), Qt.ElideRight, 92
            )
        )
        self.name_label.setToolTip(self._display_name())

    def _set_status(self, kind: str | None) -> None:
        """状态角标：None 清除；'stale' 橙 / 'error' 红（矢量警告图标）。"""
        self.status_label.clear()
        if kind is None:
            return
        color = theme_mod.percent_colors()["orange" if kind == "stale" else "red"]
        pix = glyph_pixmap("alert", color, 12)
        if pix is not None:
            self.status_label.setPixmap(pix)
        else:
            self.status_label.setText("⚠")
            self.status_label.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------ 状态渲染

    def set_result(self, result: QuotaResult | None) -> None:
        self._result = result
        if result is None:
            self._render_loading()
        elif result.ok:
            self._render_ok(result)
        elif result.error == "unsupported":
            self._render_unsupported(result)
        else:
            self._render_error(result)
        self._render_detail()

    def _render_loading(self) -> None:
        self.window_label.setText("加载中…")
        self.bar.setVisible(True)
        self.bar.set_percent(0)
        self.percent_label.setFixedWidth(38)
        self.percent_label.setText("…")
        self.reset_label.setText("")
        self._set_status(None)
        self.setToolTip("正在查询额度")

    def _render_ok(self, result: QuotaResult) -> None:
        worst = result.worst
        if worst is None:
            self._render_balance(result)
            return
        t = theme_mod.current()
        color = (
            t.fg_stale
            if result.stale
            else theme_mod.percent_colors()[
                "red" if worst.used_percent >= 90
                else "orange" if worst.used_percent >= 70
                else "green"
            ]
        )
        multi = len(result.windows) > 1
        self.window_label.setStyleSheet(f"color: {t.fg_dim}; font-size: 10px;")
        label = f"{worst.label} 等{len(result.windows)}窗" if multi else worst.label
        _elide(self.window_label, label)
        self.bar.setVisible(True)
        self.bar.set_percent(worst.used_percent)
        self.percent_label.setFixedWidth(38)
        self.percent_label.setText(f"{worst.used_percent:.0f}%")
        self.percent_label.setStyleSheet(
            f"color: {color}; font-weight: 600; font-size: 12px;"
        )

        reset = self._reset_text(result, worst.reset_in_sec)
        self.reset_label.setText(reset)
        self._set_status("stale" if result.stale else None)
        self.setToolTip(self._tooltip(result))

    def _render_balance(self, result: QuotaResult) -> None:
        """余额类（percent=None）：无进度条语义，直接展示余额金额。"""
        t = theme_mod.current()
        win = result.windows[0] if result.windows else None
        color = t.fg_stale if result.stale else t.fg_dim
        _elide(self.window_label, win.label if win else "余额")
        self.bar.setVisible(False)  # 余额类无进度语义，隐藏空进度条
        self.percent_label.setFixedWidth(64)  # 金额比百分比长（如 ¥207.37）
        _elide(self.percent_label, win.used_text if win and win.used_text else "—")
        self.percent_label.setStyleSheet(
            f"color: {color}; font-weight: 600; font-size: 12px;"
        )
        self.reset_label.setText("")
        self._set_status("stale" if result.stale else None)
        self.setToolTip(self._tooltip(result))

    def _render_error(self, result: QuotaResult) -> None:
        t = theme_mod.current()
        code = result.error or ""
        title, advice = ERROR_TEXTS.get(code, ("查询失败", ""))
        text = result.message or title
        self.window_label.setStyleSheet(f"color: {t.fg_dim}; font-size: 10px;")
        _elide(self.window_label, text)
        self.bar.setVisible(True)
        self.bar.set_percent(0)
        self.percent_label.setFixedWidth(38)
        self.percent_label.setText("—")
        self.percent_label.setStyleSheet(f"color: {t.fg_dim}; font-size: 12px;")
        self.reset_label.setText("")
        self._set_status("error")
        tip = f"{text}\n建议：{advice}" if advice else text
        self.setToolTip(tip)

    def _render_unsupported(self, result: QuotaResult) -> None:
        t = theme_mod.current()
        _elide(self.window_label, result.message or "仅消耗")
        self.window_label.setStyleSheet(f"color: {t.fg_stale}; font-size: 10px;")
        self.bar.setVisible(True)
        self.bar.set_percent(0)
        self.percent_label.setFixedWidth(38)
        self.percent_label.setText("—")
        self.percent_label.setStyleSheet(f"color: {t.fg_stale}; font-size: 12px;")
        self.reset_label.setText("")
        self._set_status(None)
        self.setToolTip(result.message or "暂不支持该类型的额度查询，展示消耗数据")

    def set_consumption(self, consumption: ConsumptionByPlan | None) -> None:
        """行详情面板的当日消耗数据（§M2 行详情：按模型拆解）。"""
        self._consumption = consumption
        self._render_detail()

    def _render_detail(self) -> None:
        result = self._result
        parts: list[str] = []
        if result and result.ok and len(result.windows) > 1:
            win_parts = []
            for w in result.windows:
                reset = self._reset_text(result, w.reset_in_sec)
                if w.used_percent is not None:
                    part = f"{w.label} {w.used_percent:.0f}%"
                    if w.used_text:
                        part += f"（{w.used_text}）"
                    if reset:
                        part += f" · {reset} 重置"
                else:
                    part = f"{w.label} {w.used_text or ''}".strip()
                win_parts.append(part)
            parts.append("额度：" + "；".join(win_parts))

        consumption = self._consumption
        if consumption is not None and (consumption.cost_usd > 0 or consumption.tokens > 0):
            if consumption.by_model:
                model_parts = [
                    f"{m.model}: ${m.cost_usd:.2f} · {m.tokens:,} tokens"
                    for m in consumption.by_model
                ]
            else:
                model_parts = [
                    f"${consumption.cost_usd:.2f} · {consumption.tokens:,} tokens"
                ]
            parts.append("消耗：" + "；".join(model_parts))

        self.detail_label.setText("\n".join(parts))

    def _reset_text(self, result: QuotaResult, reset_in_sec: int | None) -> str:
        if reset_in_sec is None:
            return ""
        remaining = reset_in_sec - max(int(time.time()) - result.fetched_at, 0)
        return fmt_duration_sec(remaining)

    def _tooltip(self, result: QuotaResult) -> str:
        lines = [result.plan_label or self.plan.name]
        for w in result.windows:
            reset = self._reset_text(result, w.reset_in_sec)
            if w.used_percent is not None:
                line = f"{w.label}: {w.used_percent:.0f}%"
                if w.used_text:
                    line += f"（{w.used_text}）"
                if reset:
                    line += f"，{reset} 后重置"
            else:
                line = f"{w.label}: {w.used_text or '—'}"
            lines.append(line)
        if result.extra:
            lines.append(result.extra)
        if result.stale:
            lines.append(
                f"更新于 {time.strftime('%H:%M', time.localtime(result.fetched_at))}"
                "（本次刷新失败，展示上次数据）"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------ 交互

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.detail_label.text():
            self._expanded = not self._expanded
            self.detail_label.setVisible(self._expanded)
            if self._expanded:
                lines = self.detail_label.text().count("\n") + 1
                self.setFixedHeight(ROW_HEIGHT + 8 + 14 * lines)
            else:
                self.setFixedHeight(ROW_HEIGHT)
        self.clicked.emit(self.plan.id)
        super().mousePressEvent(event)

    def tick(self) -> None:
        """每秒刷新倒计时；展开中的详情同步滚动，不冻结到下次 set_result。"""
        result = self._result
        if not result or not result.ok:
            return
        worst = result.worst
        if worst is not None:
            self.reset_label.setText(self._reset_text(result, worst.reset_in_sec))
        if self._expanded:
            self._render_detail()

"""主题预览：全主题 × 富场景离屏合成到 build/theme_preview.png。

覆盖全部行状态（余额/绿/橙/红/stale/错误/仅消耗 + 展开详情 + 底栏消耗），
用于主题设计与回归目视检查。用 QWidget.render 取图（无需 show、不闪窗），
不走 offscreen 平台（其字体库不全，CJK 会渲染成方框）。

用法: uv run python verify_ui.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

from token_widget.config import WidgetConfig
from token_widget.models import (
    ConsumptionByPlan,
    ConsumptionSummary,
    ModelConsumption,
    Plan,
    QuotaResult,
    QuotaWindow,
)
from token_widget.ui import theme as theme_mod
from token_widget.ui.main_window import MainWindow
from token_widget.ui.theme import ROW_HEIGHT, THEMES

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "build" / "theme_preview.png"
NOW = int(time.time())

_WIN_W = 340
_CHROME_H = 108  # 标题栏 + 底栏 + 边距（与 main_window._CHROME_HEIGHT 对齐）


def make_plan(pid, name, icon, kind, color="#30363d", current=False):
    return Plan(
        id=pid, app_type="codex", name=name, category=None, icon=icon,
        icon_color=color, is_current=current, quota_kind=kind, selected=True,
        auto_query_interval_min=5, settings_config={}, usage_script=None,
    )


def build_window() -> MainWindow:
    """富场景：7 行覆盖全部渲染分支，Kimi 行展开详情（额度 + 按模型消耗）。"""
    win = MainWindow(WidgetConfig())
    plans = [
        make_plan("ds", "DeepSeek", "deepseek", "balance"),
        make_plan("oai", "OpenAI Official", "openai", "official_sub", current=True),
        make_plan("kimi", "Kimi For Coding", "kimi", "token_plan"),
        make_plan("mm", "MiniMax", "minimax", "token_plan"),
        make_plan("ocg", "OpenCode Go", "opencode", "custom_script"),
        make_plan("ct", "Claude Team", None, "token_plan", color="#d97706"),
        make_plan("zx", "ZenMux", None, "consumption_only", color="#0ea5e9"),
    ]
    win.rebuild_rows(plans)
    rows = win.plan_list.rows

    rows["ds"].set_result(QuotaResult(
        plan_id="ds", ok=True, plan_label="DeepSeek", fetched_at=NOW,
        windows=[QuotaWindow(key="balance", label="余额 CNY",
                             used_percent=None, used_text="¥207.37")],
    ))
    rows["oai"].set_result(QuotaResult(
        plan_id="oai", ok=True, plan_label="OpenAI Official", fetched_at=NOW,
        windows=[QuotaWindow(key="quota", label="7天", used_percent=25.0,
                             reset_in_sec=4 * 86400 + 15 * 3600)],
    ))
    rows["kimi"].set_result(QuotaResult(
        plan_id="kimi", ok=True, plan_label="Kimi For Coding", fetched_at=NOW,
        windows=[
            QuotaWindow(key="5h", label="5小时", used_percent=10.0,
                        reset_in_sec=3600),
            QuotaWindow(key="weekly", label="每周", used_percent=72.0,
                        reset_in_sec=18 * 3600 + 42 * 60),
        ],
    ))
    rows["mm"].set_result(QuotaResult(
        plan_id="mm", ok=True, plan_label="MiniMax", fetched_at=NOW,
        windows=[QuotaWindow(key="5h", label="5小时", used_percent=93.0,
                             reset_in_sec=35 * 60)],
    ))
    rows["ocg"].set_result(QuotaResult(
        plan_id="ocg", ok=True, stale=True, plan_label="OpenCode Go",
        fetched_at=NOW - 300,
        windows=[QuotaWindow(key="rolling", label="5小时滚动", used_percent=45.0,
                             reset_in_sec=3 * 3600 + 7 * 60)],
    ))
    rows["ct"].set_result(QuotaResult(
        plan_id="ct", ok=False, error="fetch_failed", fetched_at=NOW,
    ))
    rows["zx"].set_result(QuotaResult(
        plan_id="zx", ok=False, error="unsupported", message="仅消耗",
        fetched_at=NOW,
    ))

    win.set_consumption(ConsumptionSummary(
        date="2026-08-04", total_cost_usd=12.34, total_tokens=1234567,
        by_plan=[ConsumptionByPlan(
            plan_id="kimi", cost_usd=1.23, tokens=45678, models=["kimi-k2"],
            by_model=[ModelConsumption(model="kimi-k2", cost_usd=1.23,
                                       tokens=45678)],
        )],
    ))

    # 展开 Kimi 行详情（多窗口 + 按模型消耗两行）
    kimi = rows["kimi"]
    kimi._expanded = True
    kimi.detail_label.setVisible(True)
    lines = kimi.detail_label.text().count("\n") + 1
    kimi.setFixedHeight(ROW_HEIGHT + 8 + 14 * lines)

    win.resize(_WIN_W, _CHROME_H + ROW_HEIGHT * 6 + ROW_HEIGHT + 8 + 14 * lines)
    return win


def grab(win) -> QImage:
    """QWidget.render 离屏取图（透明底，含自绘圆角）。"""
    img = QImage(win.size(), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    win.render(img)
    return img


def main() -> int:
    app = QApplication.instance() or QApplication([])
    grabs: dict[str, QImage] = {}
    for key in THEMES:
        theme_mod.set_current(key)
        win = build_window()
        grabs[key] = grab(win)
        win.close()
    theme_mod.set_current("dark")

    cols = 4
    label_h = 30
    pad = 14
    cell_w, cell_h = _WIN_W + pad, grabs["dark"].height() + label_h + pad
    rows = (len(THEMES) + cols - 1) // cols
    canvas = QImage(
        cols * cell_w + pad, rows * cell_h + pad,
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    canvas.fill(QColor("#3f434a"))
    p = QPainter(canvas)
    p.setFont(QFont("Segoe UI", 11))
    p.setPen(QColor("#e6e8eb"))
    for idx, (key, th) in enumerate(THEMES.items()):
        x = pad + (idx % cols) * cell_w
        y = pad + (idx // cols) * cell_h
        p.drawImage(x, y, grabs[key])
        p.drawText(x, y + grabs[key].height() + 20, f"{th.label} · {key}")
    p.end()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(OUT))
    print(f"saved: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

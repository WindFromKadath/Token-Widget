"""§6.1 视觉规范：主题系统（dark / light / glass 磨砂）。

颜色收敛到 Theme 数据类；组件一律通过 ``current()`` 取色，
主题切换后由 MainWindow.apply_theme() 重建样式，无需重启。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 用量分档颜色（<70 绿 / 70-89 橙 / >=90 红），三主题共用
PERCENT_COLORS = {
    "green": "#3fb950",
    "orange": "#d29922",
    "red": "#f85149",
}

ROW_HEIGHT = 44
WINDOW_WIDTH = 340  # 默认宽度（窗口可缩放，仅为初始值）
MIN_WINDOW_WIDTH = 300


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    bg_rgba: str       # 主窗自绘底色（paintEvent 解析的 rgba(...)）
    bg_solid: str      # 对话框/工具提示等不透明底
    fg_main: str
    fg_dim: str
    fg_stale: str
    border: str
    bar_bg: str        # 进度条轨道
    hover_bg: str      # 行/按钮悬停
    scrollbar: str
    acrylic: bool = False        # 是否尝试 DWM 亚克力磨砂
    bg_rgba_fallback: str = ""   # 磨砂不可用时的兜底底色
    percent: dict = field(default_factory=lambda: dict(PERCENT_COLORS))  # 分档色


THEMES: dict[str, Theme] = {
    "dark": Theme(
        key="dark",
        label="深色",
        bg_rgba="rgba(22, 24, 28, 246)",
        bg_solid="#16181c",
        fg_main="#e6e8eb",
        fg_dim="#8b949e",
        fg_stale="#6e7681",
        border="rgba(255, 255, 255, 0.08)",
        bar_bg="rgba(255, 255, 255, 0.10)",
        hover_bg="rgba(255, 255, 255, 0.05)",
        scrollbar="rgba(255, 255, 255, 0.22)",
    ),
    "midnight": Theme(
        key="midnight",
        label="墨蓝",
        bg_rgba="rgba(15, 20, 34, 246)",
        bg_solid="#0f1422",
        fg_main="#e2e8f0",
        fg_dim="#8b9bb4",
        fg_stale="#64748b",
        border="rgba(255, 255, 255, 0.09)",
        bar_bg="rgba(255, 255, 255, 0.10)",
        hover_bg="rgba(255, 255, 255, 0.05)",
        scrollbar="rgba(255, 255, 255, 0.22)",
    ),
    "nord": Theme(
        key="nord",
        label="北欧",
        bg_rgba="rgba(46, 52, 64, 246)",
        bg_solid="#2e3440",
        fg_main="#eceff4",
        fg_dim="#a5adc4",
        fg_stale="#6d7a94",
        border="rgba(236, 239, 244, 0.10)",
        bar_bg="rgba(236, 239, 244, 0.12)",
        hover_bg="rgba(236, 239, 244, 0.06)",
        scrollbar="rgba(236, 239, 244, 0.25)",
        percent={"green": "#a3be8c", "orange": "#ebcb8b", "red": "#bf616a"},
    ),
    "oled": Theme(
        key="oled",
        label="纯黑 OLED",
        bg_rgba="rgba(0, 0, 0, 246)",
        bg_solid="#000000",
        fg_main="#f2f2f2",
        fg_dim="#a3a3a3",
        fg_stale="#737373",
        border="rgba(255, 255, 255, 0.16)",
        bar_bg="rgba(255, 255, 255, 0.14)",
        hover_bg="rgba(255, 255, 255, 0.07)",
        scrollbar="rgba(255, 255, 255, 0.28)",
        # 纯黑底上需要更高亮度的分档色
        percent={"green": "#56d364", "orange": "#e3b341", "red": "#ff7b72"},
    ),
    "light": Theme(
        key="light",
        label="浅色",
        bg_rgba="rgba(248, 249, 251, 246)",
        bg_solid="#f4f5f7",
        fg_main="#1f2328",
        fg_dim="#57606a",
        fg_stale="#8b949e",
        border="rgba(0, 0, 0, 0.10)",
        bar_bg="rgba(0, 0, 0, 0.08)",
        hover_bg="rgba(0, 0, 0, 0.05)",
        scrollbar="rgba(0, 0, 0, 0.25)",
        percent={"green": "#1a7f37", "orange": "#9a6700", "red": "#cf222e"},
    ),
    "paper": Theme(
        key="paper",
        label="纸张",
        bg_rgba="rgba(247, 243, 234, 246)",
        bg_solid="#f7f3ea",
        fg_main="#403c33",
        fg_dim="#7d7666",
        fg_stale="#9c9484",
        border="rgba(60, 54, 40, 0.12)",
        bar_bg="rgba(60, 54, 40, 0.10)",
        hover_bg="rgba(60, 54, 40, 0.06)",
        scrollbar="rgba(60, 54, 40, 0.28)",
        # 暖纸底上适度压深的分档色
        percent={"green": "#2f7d3a", "orange": "#a86f00", "red": "#c0392b"},
    ),
    "glass": Theme(
        key="glass",
        label="磨砂玻璃",
        # 亚克力生效时底色低透明，让模糊透出来；失败时退回高不透明深色
        bg_rgba="rgba(24, 26, 32, 112)",
        bg_rgba_fallback="rgba(22, 24, 28, 225)",
        bg_solid="#16181c",
        fg_main="#f5f7fa",
        fg_dim="#d3d9e0",
        fg_stale="#aab1bb",
        border="rgba(255, 255, 255, 0.16)",
        bar_bg="rgba(255, 255, 255, 0.18)",
        hover_bg="rgba(255, 255, 255, 0.10)",
        scrollbar="rgba(255, 255, 255, 0.30)",
        acrylic=True,
        percent={"green": "#56d364", "orange": "#e3b341", "red": "#ff7b72"},
    ),
}


def percent_colors() -> dict:
    """当前主题的分档色（light 更深、glass 更亮，保证对比度）。"""
    return _current.percent

_current: Theme = THEMES["dark"]


def current() -> Theme:
    return _current


def set_current(key: str) -> Theme:
    """切换当前主题；未知 key 回退 dark。返回生效主题。"""
    global _current
    _current = THEMES.get(key, THEMES["dark"])
    return _current


def qss_base() -> str:
    t = _current
    return f"""
    QWidget {{
        color: {t.fg_main};
        font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
        font-size: 12px;
    }}
    QToolTip {{
        background-color: {t.bg_solid};
        color: {t.fg_main};
        border: 1px solid {t.border};
        padding: 4px 6px;
    }}
    QPushButton {{
        background: transparent;
        border: none;
        padding: 2px 6px;
        color: {t.fg_dim};
        border-radius: 6px;
    }}
    QPushButton:hover {{ color: {t.fg_main}; background: {t.hover_bg}; }}
    QPushButton:checked {{ color: {t.fg_main}; }}
    QCheckBox {{ spacing: 6px; }}
    QScrollArea {{ border: none; }}
    QScrollBar:vertical {{
        background: transparent; width: 8px; margin: 2px 1px;
    }}
    QScrollBar::handle:vertical {{
        background: {t.scrollbar}; border-radius: 4px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{ height: 0; border: none; }}
    #planRow {{ border-radius: 8px; }}
    #planRow:hover {{ background: {t.hover_bg}; }}
    """


def rgba_tuple(value: str) -> tuple[int, int, int, int]:
    """解析 'rgba(r, g, b, a)' 为元组（paintEvent 用）。"""
    inner = value[value.index("(") + 1 : value.rindex(")")]
    parts = [p.strip() for p in inner.split(",")]
    return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])

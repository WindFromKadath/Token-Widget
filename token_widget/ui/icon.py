"""供应商图标加载（CC Switch 同款 SVG 资产）。

资产来自 CC Switch（MIT, farion1231/cc-switch）`src/icons/extracted/`，
vendored 到 `token_widget/assets/icons/`；providers.icon 名 -> 资产文件，
个别名称与资产不一致时走 _ALIASES。加载失败回退 None（调用方显示首字母）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme as theme_mod

ICONS_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"

# DB providers.icon 名 -> 资产文件名（无别名时用同名小写）
_ALIASES = {
    "opencode": "opencode-logo-light",
}

_CACHE: dict[str, QPixmap | None] = {}


def icon_pixmap(name: str | None, size: int = 16) -> QPixmap | None:
    """按 icon 名加载 SVG 并缩放；失败返回 None（不抛异常）。"""
    if not name:
        return None
    # currentColor 图标按主题前景色渲染，缓存键随主题区分（切换后重取）
    theme_key = theme_mod.current().key
    key = f"{name}|{size}|{theme_key}"
    if key in _CACHE:
        return _CACHE[key]
    result: QPixmap | None = None
    # 无 GUI 环境（测试/脚本）下 QPixmap 会触发 Qt abort，先探测实例
    if QGuiApplication.instance() is not None:
        file_name = _ALIASES.get(name.lower(), name.lower())
        path = ICONS_DIR / f"{file_name}.svg"
        if path.exists():
            try:
                # 部分 logo（如 openai.svg）fill=currentColor，直接渲染为黑色，
                # 深色底上不可见：替换为当前主题前景色再加载
                raw = path.read_bytes().replace(
                    b"currentColor", theme_mod.current().fg_main.encode("ascii")
                )
                pix = QPixmap()
                if pix.loadFromData(raw, "svg") and not pix.isNull():
                    result = pix.scaled(
                        size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
            except Exception:  # SVG 插件缺失等：回退首字母
                result = None
    _CACHE[key] = result
    return result


# ---------------------------------------------------------------- 内置矢量 glyph
# 描边风格（lucide 风格，24x24 viewBox），颜色由调用方注入，随主题变化

_GLYPH_PATHS = {
    "menu": '<line x1="4" x2="20" y1="6" y2="6"/>'
            '<line x1="4" x2="20" y1="12" y2="12"/>'
            '<line x1="4" x2="20" y1="18" y2="18"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>'
               '<path d="M21 3v5h-5"/>'
               '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>'
               '<path d="M3 21v-5h5"/>',
    "pin": '<path d="M12 17v5"/>'
           '<path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16'
           "a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9"
           'A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z"/>',
    "close": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "sliders": '<line x1="21" x2="14" y1="4" y2="4"/>'
               '<line x1="10" x2="3" y1="4" y2="4"/>'
               '<line x1="21" x2="12" y1="12" y2="12"/>'
               '<line x1="8" x2="3" y1="12" y2="12"/>'
               '<line x1="21" x2="16" y1="20" y2="20"/>'
               '<line x1="12" x2="3" y1="20" y2="20"/>'
               '<line x1="14" x2="14" y1="2" y2="6"/>'
               '<line x1="8" x2="8" y1="10" y2="14"/>'
               '<line x1="16" x2="16" y1="18" y2="22"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16'
             'a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
}

_GLYPH_CACHE: dict[str, QPixmap | None] = {}


def glyph_pixmap(name: str, color: str, size: int = 14) -> QPixmap | None:
    """按名称+颜色渲染内置矢量图标；失败返回 None（调用方可回退文本）。"""
    paths = _GLYPH_PATHS.get(name)
    if paths is None:
        return None
    key = f"{name}|{color}|{size}"
    if key in _GLYPH_CACHE:
        return _GLYPH_CACHE[key]
    result: QPixmap | None = None
    if QGuiApplication.instance() is not None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
            ' fill="none" stroke="' + color + '" stroke-width="2"'
            ' stroke-linecap="round" stroke-linejoin="round">'
            + paths + "</svg>"
        )
        try:
            pix = QPixmap()
            if pix.loadFromData(svg.encode("utf-8"), "svg") and not pix.isNull():
                result = pix.scaled(
                    size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
        except Exception:
            result = None
    _GLYPH_CACHE[key] = result
    return result


# ---------------------------------------------------------------- 应用图标
# assets/app.svg（tools/make_icon.py 生成，方案 A「微缩挂件」），
# 供托盘基础图标 / 窗口图标使用；多尺寸渲染保证各 DPI 清晰

APP_ICON_PATH = ICONS_DIR.parent / "app.svg"
_APP_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
_app_icon_cache: QIcon | None = None
_app_icon_resolved = False


def app_icon() -> QIcon | None:
    """应用图标（多尺寸 QIcon）；无 GUI 环境或加载失败返回 None。"""
    global _app_icon_cache, _app_icon_resolved
    if _app_icon_resolved:
        return _app_icon_cache
    _app_icon_resolved = True
    icon: QIcon | None = None
    if QGuiApplication.instance() is not None and APP_ICON_PATH.exists():
        try:
            raw = APP_ICON_PATH.read_bytes()
            renderer = QSvgRenderer(raw)
            icon = QIcon()
            for size in _APP_ICON_SIZES:
                pix = QPixmap(size, size)
                pix.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pix)
                renderer.render(painter)
                painter.end()
                icon.addPixmap(pix)
            if icon.isNull():
                icon = None
        except Exception:  # SVG 渲染失败等：托盘回退圆点
            icon = None
    _app_icon_cache = icon
    return icon

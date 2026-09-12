"""应用图标生成：SVG 源 -> 多尺寸 ICO + 候选方案预览图。

用法：
    uv run python tools/make_icon.py            # 只生成候选预览 build/icon_preview.png
    uv run python tools/make_icon.py A          # 追加产出选定方案的 assets/app.svg + app.ico

设计基调取自主题（theme.py）：深色圆角瓦片 #16181c + 分档色绿/橙。
ICO 为 Vista+ 的 PNG 载荷多尺寸容器（16/24/32/48/64/128/256）。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "token_widget" / "assets"
PREVIEW = ROOT / "build" / "icon_preview.png"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

_TILE = (
    '<rect x="2" y="2" width="60" height="60" rx="14" fill="#16181c"'
    ' stroke="#ffffff" stroke-opacity="0.10" stroke-width="1.5"/>'
)

# 方案 A「微缩挂件」：两条额度条（绿 65% / 橙 86%），挂件 UI 的缩影
_SVG_A = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    + _TILE
    + '<rect x="14" y="21" width="36" height="7" rx="3.5" fill="#ffffff" fill-opacity="0.12"/>'
      '<rect x="14" y="21" width="24" height="7" rx="3.5" fill="#3fb950"/>'
      '<rect x="14" y="36" width="36" height="7" rx="3.5" fill="#ffffff" fill-opacity="0.12"/>'
      '<rect x="14" y="36" width="31" height="7" rx="3.5" fill="#d29922"/>'
      "</svg>"
)

# 方案 B「额度环」：环形仪表，绿色弧线扫过 65%
_SVG_B = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    + _TILE
    + '<circle cx="32" cy="32" r="16" fill="none" stroke="#ffffff"'
      ' stroke-opacity="0.12" stroke-width="7"/>'
      '<path d="M 32 16 A 16 16 0 1 1 19.06 41.40" fill="none"'
      ' stroke="#3fb950" stroke-width="7" stroke-linecap="round"/>'
      "</svg>"
)

# 方案 C「Token 币」：绿色圆形徽章 + 深色 T 字
_SVG_C = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    + _TILE
    + '<circle cx="32" cy="32" r="17" fill="#3fb950"/>'
      '<path d="M22 26 h20 v6 h-7 v14 h-6 V32 h-7 z" fill="#16181c"/>'
      "</svg>"
)

VARIANTS = {"A": _SVG_A, "B": _SVG_B, "C": _SVG_C}


def render(svg: str, size: int):
    """QSvgRenderer 矢量渲染到 size×size QImage（透明底）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(svg.encode("utf-8"))
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return image


def png_bytes(image) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


def pack_ico(svg: str, path: Path) -> None:
    """多尺寸 PNG 载荷 ICO（宽度/高度字段 256 记为 0）。"""
    payloads = [(s, png_bytes(render(svg, s))) for s in ICO_SIZES]
    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = 6 + 16 * len(payloads)
    entries = body = b""
    for size, data in payloads:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        body += data
        offset += len(data)
    path.write_bytes(header + entries + body)


def make_preview(path: Path) -> None:
    """3 方案 × (96/48/32/16 实尺寸 + 16 最近邻放大 4x) 对比图。"""
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    row_h, pad = 128, 16
    cols = [96, 48, 32, 16, 64]  # 最后一列是 16px 的 4 倍最近邻放大
    width = pad * (len(cols) + 2) + 64 + sum(cols)
    height = pad * (len(VARIANTS) + 1) + row_h * len(VARIANTS)
    canvas = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QColor("#808080"))
    p = QPainter(canvas)
    p.setFont(QFont("Segoe UI", 14))

    for row, (name, svg) in enumerate(VARIANTS.items()):
        y = pad + row * (row_h + pad)
        p.setPen(QColor("#ffffff"))
        p.drawText(pad, y + row_h // 2 + 5, name)
        x = pad * 2 + 32
        for col, size in enumerate(cols):
            cy = y + (row_h - size) // 2
            if col == len(cols) - 1:  # 16px 最近邻放大，检验托盘实尺寸观感
                from PySide6.QtCore import Qt

                small = render(svg, 16).scaled(
                    size, size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
                p.drawImage(x, cy, small)
            else:
                p.drawImage(x, cy, render(svg, size))
            x += size + pad
    p.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(path))


def main() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    make_preview(PREVIEW)
    print(f"预览: {PREVIEW}")
    if len(sys.argv) > 1:
        key = sys.argv[1].upper()
        svg = VARIANTS.get(key)
        if svg is None:
            raise SystemExit(f"未知方案 {key!r}（可选 {''.join(VARIANTS)}）")
        (ASSETS / "app.svg").write_text(svg, encoding="utf-8")
        pack_ico(svg, ASSETS / "app.ico")
        print(f"产出: {ASSETS / 'app.svg'}, {ASSETS / 'app.ico'}")


if __name__ == "__main__":
    main()

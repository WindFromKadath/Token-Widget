"""设置对话框（§6.1 扩展）：主题选择，选中即实时预览。

与托盘"主题"子菜单共用同一条切换链路（app._on_theme_selected），
两边勾选状态自动同步。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
)

from . import theme as theme_mod
from .picker import dialog_qss
from .theme import THEMES

_THEME_DESC = {
    "dark": "纯深色底，96% 不透明",
    "midnight": "深蓝夜色底，柔和低蓝光",
    "nord": "Nord 极地配色，低饱和冷灰蓝",
    "oled": "纯黑底，OLED 屏高对比省电",
    "light": "浅色底、深色文字与图标",
    "paper": "暖纸色底，护眼浅色系",
    "glass": "Windows 亚克力磨砂（不支持时退回深色）",
}


class SettingsDialog(QDialog):
    """简单设置：主题单选，切换立即生效并持久化（由调用方处理）。"""

    theme_selected = Signal(str)

    def __init__(self, current: str = "dark", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(300)
        self.setStyleSheet(dialog_qss())

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        header = QLabel("主题")
        header.setStyleSheet(
            f"color: {theme_mod.current().fg_dim}; font-weight: 600;"
        )
        layout.addWidget(header)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._radios: dict[str, QRadioButton] = {}
        self._dim_labels: list[QLabel] = [header]
        for key, th in THEMES.items():
            radio = QRadioButton(th.label)
            radio.setChecked(key == current)
            self._group.addButton(radio)
            self._radios[key] = radio
            layout.addWidget(radio)
            desc = QLabel(_THEME_DESC.get(key, ""))
            desc.setStyleSheet(
                f"color: {theme_mod.current().fg_dim};"
                " font-size: 10px; padding-left: 22px;"
            )
            self._dim_labels.append(desc)
            layout.addWidget(desc)
            radio.toggled.connect(
                lambda checked, k=key: checked and self.theme_selected.emit(k)
            )

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # 在对话框内切换主题时，对话框自身也即时换肤（不只主窗预览）
        self.theme_selected.connect(lambda _key: self._restyle())

    def _restyle(self) -> None:
        self.setStyleSheet(dialog_qss())
        dim = theme_mod.current().fg_dim
        self._dim_labels[0].setStyleSheet(f"color: {dim}; font-weight: 600;")
        for label in self._dim_labels[1:]:
            label.setStyleSheet(
                f"color: {dim}; font-size: 10px; padding-left: 22px;"
            )

    def selected_theme(self) -> str:
        for key, radio in self._radios.items():
            if radio.isChecked():
                return key
        return "dark"

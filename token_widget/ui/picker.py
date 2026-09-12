"""Plan 勾选面板（§6.2）与 OpenCode Go 凭据输入。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..collectors import is_opencode_go_native
from ..config import OpencodeGoAccount, WidgetConfig, save as save_config
from ..models import Plan
from . import theme as theme_mod
from .icon import icon_pixmap
from .theme import qss_base


def dialog_qss() -> str:
    return qss_base() + f"QDialog {{ background: {theme_mod.current().bg_solid}; }}"

_APP_TYPE_LABELS = {
    "claude": "Claude",
    "claude-desktop": "Claude Desktop",
    "codex": "Codex",
    "gemini": "Gemini",
    "opencode": "OpenCode",
}

_KIND_LABELS = {
    "official_sub": "官方订阅",
    "consumption_only": "仅消耗",
    "token_plan": "Token Plan",
    "custom_script": "自定义脚本",
    "balance": "余额",
}

OPENCODE_HINT = (
    "优先使用内嵌登录自动捕获；若需手动粘贴：浏览器登录 opencode.ai → "
    "F12 → Application → Cookies → https://opencode.ai → 复制 auth 的值；"
    "workspace_id 取地址栏跳转后 /workspace/wrk_XXXX/ 段（必须 wrk_ 前缀）。"
)


class OpencodeCredentialsDialog(QDialog):
    def __init__(self, current: OpencodeGoAccount | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OpenCode Go 凭据")
        self.setMinimumWidth(420)
        self.setStyleSheet(dialog_qss())
        layout = QVBoxLayout(self)

        hint = QLabel(OPENCODE_HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme_mod.current().fg_dim};")
        layout.addWidget(hint)

        form = QFormLayout()
        self.cookie_edit = QLineEdit()
        self.cookie_edit.setEchoMode(QLineEdit.Password)
        self.cookie_edit.setPlaceholderText("auth cookie 值")
        self.workspace_edit = QLineEdit()
        self.workspace_edit.setPlaceholderText("wrk_…")
        if current:
            self.cookie_edit.setText(current.auth_cookie)
            self.workspace_edit.setText(current.workspace_id)
        form.addRow("auth cookie", self.cookie_edit)
        form.addRow("workspace_id", self.workspace_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def account(self) -> OpencodeGoAccount:
        return OpencodeGoAccount(
            auth_cookie=self.cookie_edit.text().strip(),
            workspace_id=self.workspace_edit.text().strip(),
        )


class PickerDialog(QDialog):
    """按 appType 分组列出全部 Plan，勾选即写挂件 config。"""

    opencode_setup_requested = Signal()

    def __init__(self, plans: list[Plan], config: WidgetConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择套餐")
        self.setMinimumWidth(420)
        self.setStyleSheet(dialog_qss())
        self._config = config
        self._plans = plans

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        if not plans:
            layout.addWidget(QLabel("CC Switch 中没有可用供应商。"))
        by_app: dict[str, list[Plan]] = {}
        for plan in plans:
            by_app.setdefault(plan.app_type, []).append(plan)
        for app_type, group in by_app.items():
            header = QLabel(_APP_TYPE_LABELS.get(app_type, app_type))
            header.setStyleSheet(
                f"color: {theme_mod.current().fg_dim}; font-weight: 600;"
            )
            layout.addWidget(header)
            for plan in group:
                layout.addLayout(self._plan_row(plan))

        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        # Close 是 RejectRole，只接 rejected（再挂 clicked->accept 会一次触发两个）
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _plan_row(self, plan: Plan) -> QVBoxLayout:
        row = QHBoxLayout()
        icon = icon_pixmap(plan.icon, 16)
        if icon is not None:
            icon_label = QLabel()
            icon_label.setPixmap(icon)
            icon_label.setFixedSize(16, 16)
            row.addWidget(icon_label)
        box = QCheckBox(plan.name)
        box.setChecked(plan.selected)
        box.toggled.connect(lambda checked, pid=plan.id: self._on_toggle(pid, checked))
        row.addWidget(box)

        badge = QLabel(_KIND_LABELS.get(plan.quota_kind, plan.quota_kind))
        badge.setStyleSheet(
            f"color: {theme_mod.current().fg_dim};"
            f" border: 1px solid {theme_mod.current().border};"
            "border-radius: 4px; padding: 1px 6px; font-size: 10px;"
        )
        row.addWidget(badge)
        if plan.category:
            cat = QLabel(plan.category)
            cat.setStyleSheet(
                f"color: {theme_mod.current().fg_dim}; font-size: 10px;"
            )
            row.addWidget(cat)
        row.addStretch(1)

        if is_opencode_go_native(plan):
            account = self._config.default_opencode_account_data
            btn = QPushButton("重设凭据" if account and account.ready else "设置凭据")
            btn.clicked.connect(self.opencode_setup_requested.emit)
            row.addWidget(btn)

        wrapper = QVBoxLayout()
        wrapper.setSpacing(2)
        wrapper.addLayout(row)
        return wrapper

    def _on_toggle(self, plan_id: str, checked: bool) -> None:
        ids = set(self._config.selected_plan_ids)
        if checked:
            ids.add(plan_id)
        else:
            ids.discard(plan_id)
        self._config.selected_plan_ids = sorted(ids)
        save_config(self._config)
        for plan in self._plans:
            if plan.id == plan_id:
                plan.selected = checked

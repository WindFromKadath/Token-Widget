"""OpenCode Go 内嵌登录对话框（M3 §4.4 凭据一键重登）。

QtWebEngine 打开 opencode.ai，登录后自动捕获：
- `auth` cookie（cookieStore.cookieAdded，域 opencode.ai）
- workspace_id（urlChanged 正则识别 `/workspace/wrk_XXX/` 段）

持久化 profile（"opencode-login"）：再次打开复用已登录会话，无需重新登录。
对话框内保留"手动粘贴凭据"兜底入口。

注意：模块顶层**不**导入 QtWebEngine（惰性导入），无 WebEngine 环境
（测试、降级路径）仍可导入纯函数。
"""

from __future__ import annotations

import re

from PySide6.QtCore import QUrl, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..config import OpencodeGoAccount
from .picker import OpencodeCredentialsDialog, dialog_qss

OPENCODE_HOME = "https://opencode.ai/"
_WORKSPACE_RE = re.compile(r"https?://opencode\.ai/workspace/(wrk_[A-Za-z0-9]+)")


# ---------------------------------------------------------------- 纯函数（可测）

def extract_workspace_id(url: str) -> str | None:
    """从 opencode.ai 的工作区 URL 提取 ID（/workspace/wrk_XXX/ 段）；其他返回 None。"""
    m = _WORKSPACE_RE.search(url)
    return m.group(1) if m else None


def is_auth_cookie(name: str, domain: str) -> bool:
    """auth cookie 判定：cookie 名为 auth 且域为根域 opencode.ai。

    只认根域（opencode.ai / .opencode.ai）：auth.opencode.ai 授权页写入的
    匿名占位 cookie（子域）会被排除，避免误采。
    """
    if name != "auth":
        return False
    return (domain or "").lstrip(".") == "opencode.ai"


# ---------------------------------------------------------------- 对话框

class WebviewLoginDialog(QDialog):
    """内嵌登录：webview 登录 opencode.ai，自动捕获 auth cookie 与 workspace_id。"""

    def __init__(self, current: OpencodeGoAccount | None, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self._auth_cookie: str | None = None
        self._captured_workspace: str | None = None

        self.setWindowTitle("OpenCode Go 登录")
        self.setMinimumSize(880, 640)
        self.setStyleSheet(dialog_qss())

        self._profile = QWebEngineProfile("opencode-login", self)
        self._page = QWebEnginePage(self._profile, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self._page)
        self._profile.cookieStore().cookieAdded.connect(self._on_cookie_added)
        self.view.urlChanged.connect(self._on_url_changed)
        self.view.loadFinished.connect(self._on_load_finished)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        hint = QLabel(
            "OpenCode 已改用官方 OAuth 会话，登录 cookie 会随会话定期失效，"
            "需要重新登录属正常现象。\n登录完成后本窗口会自动捕获登录态（auth cookie）"
            "与工作区 ID，点「保存」即可恢复额度显示。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        layout.addWidget(hint)

        self.status_label = QLabel("请在下方窗口登录 opencode.ai（登录后将自动捕获登录态）")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addWidget(self.view, 1)

        ws_row = QHBoxLayout()
        ws_row.addWidget(QLabel("workspace_id"))
        self.workspace_edit = QLineEdit()
        self.workspace_edit.setPlaceholderText(
            "登录后打开你的工作区页面将自动识别，也可手动填写"
        )
        if current:
            self.workspace_edit.setText(current.workspace_id or "")
        ws_row.addWidget(self.workspace_edit, 1)
        layout.addLayout(ws_row)

        btn_row = QHBoxLayout()
        btn_home = QPushButton("↻ 回首页")
        btn_home.setToolTip("页面卡住或出现 OAuth 错误时，回到 opencode.ai 首页重新进入")
        btn_home.clicked.connect(lambda: self.view.setUrl(QUrl(OPENCODE_HOME)))
        btn_clear = QPushButton("清除登录态")
        btn_clear.setToolTip(
            "报 cookie / unknown state 错误时使用：清空已保存的会话与缓存，从头登录"
        )
        btn_clear.clicked.connect(self._clear_session)
        btn_manual = QPushButton("手动粘贴凭据…")
        btn_manual.clicked.connect(self._open_manual)
        self.btn_save = QPushButton("保存")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.accept)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_home)
        btn_row.addWidget(btn_clear)
        btn_row.addWidget(btn_manual)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

        # 总是从首页进入：深链 /workspace/... 在会话过期时会重放一次性 OAuth
        # code（页面报 "Invalid authorization code" 卡死）；首页会自行发起新流程
        self.view.setUrl(QUrl(OPENCODE_HOME))
        # 打开即扫描：持久化 profile 里已有有效会话时立即捕获、可直接保存
        self._profile.cookieStore().loadAllCookies()

    # ------------------------------------------------------------ 捕获

    def _on_cookie_added(self, cookie) -> None:
        name = bytes(cookie.name()).decode("utf-8", errors="replace")
        # 域名判定已排除授权页占位 cookie；不再要求视图 URL 在 opencode.ai
        # （会话 cookie 恰好在 OAuth 回调跳转途中写入，那时视图还停在授权页）
        if not is_auth_cookie(name, cookie.domain() or ""):
            return
        value = bytes(cookie.value()).decode("utf-8", errors="replace")
        if value:
            self._auth_cookie = value
            self._update_status()

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            # 登录态可能复用自持久化 profile（不触发 cookieAdded）：全量扫描兜底
            self._profile.cookieStore().loadAllCookies()

    def _on_url_changed(self, url) -> None:
        wid = extract_workspace_id(url.toString())
        if wid:
            self._captured_workspace = wid
            self.workspace_edit.setText(wid)
            self._update_status()

    def _update_status(self) -> None:
        self.btn_save.setEnabled(bool(self._auth_cookie))
        if not self._auth_cookie:
            self.status_label.setText(
                "请在下方窗口登录 opencode.ai（登录后将自动捕获登录态）"
            )
            return
        ws = self.workspace_edit.text().strip()
        if ws:
            self.status_label.setText(f"已捕获登录态 ✓ · workspace: {ws}")
        else:
            self.status_label.setText(
                "已捕获登录态 ✓（在窗口中打开你的工作区页面以自动识别 workspace_id）"
            )

    # ------------------------------------------------------------ 兜底

    def _clear_session(self) -> None:
        """清空持久化会话与缓存后回首页（OAuth state/cookie 损坏时的一键恢复）。"""
        self._profile.cookieStore().deleteAllCookies()
        self._profile.clearHttpCache()
        self._auth_cookie = None
        self._update_status()
        self.view.setUrl(QUrl(OPENCODE_HOME))

    def _open_manual(self) -> None:
        current = OpencodeGoAccount(
            auth_cookie=self._auth_cookie or "",
            workspace_id=self.workspace_edit.text().strip(),
        )
        dialog = OpencodeCredentialsDialog(current, self)
        if dialog.exec():
            self._auth_cookie = dialog.account.auth_cookie or None
            self.workspace_edit.setText(dialog.account.workspace_id)
            self._update_status()

    @property
    def account(self) -> OpencodeGoAccount:
        return OpencodeGoAccount(
            auth_cookie=self._auth_cookie or "",
            workspace_id=self.workspace_edit.text().strip(),
        )

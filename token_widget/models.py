"""统一数据契约（DESIGN.md §2）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

QuotaKind = Literal[
    "custom_script",
    "token_plan",
    "official_sub",
    "balance",
    "consumption_only",
]

ErrorCode = Literal[
    "cookie_expired",
    "no_subscription",
    "auth_failed",
    "fetch_failed",
    "parse_failed",
    "script_failed",
    "db_locked",
    "unsupported",
    "upstream_error",
    "oauth_login_required",
]

# §8 错误文案表：code -> (UI 文案, 建议动作)
ERROR_TEXTS: dict[str, tuple[str, str]] = {
    "cookie_expired": (
        "凭据失效（或工作区 ID 有误）",
        "自动弹出一键重登（内嵌 webview 重新登录）；OpenCode 会话 cookie 随官方 OAuth 会话定期失效，属正常现象",
    ),
    "no_subscription": ("该账号无此套餐", "确认登录账号"),
    "auth_failed": ("API Key 无效", "去 CC Switch 检查该供应商 Key"),
    "fetch_failed": ("网络异常，展示上次数据", "自动恢复，无需操作"),
    "parse_failed": ("上游结构变化，等待适配更新", "反馈 issue"),
    "script_failed": ("用量脚本执行失败", "在 CC Switch 里“测试脚本”核对"),
    "db_locked": ("CC Switch 数据库暂不可读", "自动重试"),
    "unsupported": ("暂不支持该类型的额度查询", "展示消耗数据"),
    "upstream_error": ("上游业务返回错误", "按返回信息处理；多为凭据或额度问题"),
    "oauth_login_required": (
        "官方账号未登录或凭据失效",
        "用官方 CLI 重新登录（claude login / codex login / gemini login）后重试",
    ),
}


def classify_quota_kind(category: str | None, usage_script: dict | None) -> QuotaKind:
    """templateType 路由规则（§3.4）。"""
    us = usage_script or {}
    enabled = bool(us.get("enabled"))
    template = us.get("templateType")
    code = us.get("code")
    if enabled and template == "token_plan":
        return "token_plan"
    if enabled and template == "balance":
        return "balance"  # 原生余额模板（DeepSeek 等，无 code，走内置采集器）
    if enabled and template in (None, "custom") and code:
        return "custom_script"
    if category == "official":
        return "official_sub"
    return "consumption_only"


@dataclass
class Plan:
    """一个可展示的套餐（providers 表 + 挂件勾选状态）。"""

    id: str
    app_type: str
    name: str
    category: str | None
    icon: str | None
    icon_color: str | None
    is_current: bool
    quota_kind: QuotaKind
    selected: bool = False
    auto_query_interval_min: int = 5
    # 含 API key 等敏感信息：只允许内存使用，repr 中隐藏（§9）
    settings_config: dict = field(default_factory=dict, repr=False)
    usage_script: dict | None = field(default=None, repr=False)


@dataclass
class QuotaWindow:
    """单个时间窗口的额度。

    used_percent 为 None 表示余额类窗口（无总量概念，如 DeepSeek 账户余额），
    行内以 used_text 展示余额金额；此类窗口不参与 worst 排序。
    """

    key: str
    label: str
    used_percent: float | None = None
    reset_in_sec: int | None = None
    used_text: str | None = None


@dataclass
class QuotaResult:
    """所有采集器的归一化输出。"""

    plan_id: str
    ok: bool
    windows: list[QuotaWindow] = field(default_factory=list)
    plan_label: str | None = None
    extra: str | None = None
    fetched_at: int = 0
    stale: bool = False
    error: ErrorCode | None = None
    message: str | None = None  # invalidMessage 等展示文本

    @property
    def worst(self) -> QuotaWindow | None:
        """最紧张的窗口（used_percent 最大）；余额类窗口（percent=None）不参与。"""
        scored = [w for w in self.windows if w.used_percent is not None]
        return max(scored, key=lambda w: w.used_percent, default=None)


@dataclass
class ModelConsumption:
    """单个模型的当日消耗（usage_daily_rollups 按 model 聚合）。"""

    model: str
    cost_usd: float
    tokens: int


@dataclass
class ConsumptionByPlan:
    plan_id: str
    cost_usd: float
    tokens: int
    models: list[str] = field(default_factory=list)
    by_model: list[ModelConsumption] = field(default_factory=list)


@dataclass
class ConsumptionSummary:
    """当日消耗（来自 usage_daily_rollups）。"""

    date: str
    total_cost_usd: float
    total_tokens: int
    by_plan: list[ConsumptionByPlan] = field(default_factory=list)


def fmt_duration_sec(sec: int | None) -> str:
    """秒数 -> '6d13h' / '5h3m' / '45m' 形式；None -> '?'。"""
    if sec is None:
        return "?"
    sec = max(int(sec), 0)
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def percent_color_class(used_percent: float) -> str:
    """§6.1 颜色分档：<70 绿 / 70-89 橙 / >=90 红。"""
    if used_percent >= 90:
        return "red"
    if used_percent >= 70:
        return "orange"
    return "green"

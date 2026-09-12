"""采集器路由（DESIGN.md §4.1）。

quota_kind -> 采集器：
- custom_script    → §4.2 Node runner；OpenCode Go 例外走 §4.4 原生实现
- token_plan       → §4.3 按 codingPlanProvider 分发（kimi / zhipu / zhipu_team
                     / minimax / zenmux 已实现；volcengine 为 M2+）
- official_sub     → §4.5 官方 CLI OAuth 凭据查询（claude / codex / gemini）
- balance          → §4.7 原生余额（DeepSeek / StepFun / SiliconFlow /
                     OpenRouter / Novita AI）
- consumption_only → None（不发请求，UI 仅展示消耗）
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .. import credentials
from ..config import WidgetConfig
from ..models import Plan, QuotaResult
from ..store import Store
from . import balance, custom_script, kimi, official_sub, opencode_go, token_plan
from .base import CollectorError

Collector = Callable[[], QuotaResult]

# token_plan base_url 兜底路由（§4.3，与 CC Switch detect_provider 子串一致）：
# volcengine 为 M2+，仍标记 unsupported
_BASE_URL_ROUTES = (
    ("api.kimi.com/coding", "kimi"),
    ("bigmodel.cn", "zhipu"),
    ("api.z.ai", "zhipu"),
    ("api.minimaxi.com", "minimax"),
    ("api.minimax.io", "minimax"),
    ("zenmux", "zenmux"),
    ("volces.com/api/coding", "volcengine"),
)


def is_opencode_go_native(plan: Plan) -> bool:
    """库中 OpenCode Go 供应商的脚本指向本地桥接服务（127.0.0.1:18443）；
    挂件内化采集逻辑（§4.4），不再依赖桥接进程常驻。"""
    if "opencode" in plan.name.lower():
        return True
    code = (plan.usage_script or {}).get("code") or ""
    return "127.0.0.1:18443" in code or "localhost:18443" in code


def _unsupported(plan_id: str, message: str | None = None) -> QuotaResult:
    return QuotaResult(
        plan_id=plan_id,
        ok=False,
        error="unsupported",
        message=message,
        fetched_at=int(time.time()),
    )


def build_collector(
    plan: Plan,
    config: WidgetConfig,
    store: Store | None = None,
) -> Collector | None:
    """返回可调用采集器；consumption_only 返回 None（不请求网络）。

    采集器成功返回 QuotaResult，失败抛 CollectorError(code)。
    """
    if plan.quota_kind == "consumption_only":
        return None

    if plan.quota_kind == "balance":
        # §4.7 原生余额模板（DeepSeek 等）：内置采集器按 base_url 识别供应商
        endpoint = store.endpoint_url(plan.id) if store else None
        creds = credentials.resolve(plan, endpoint)
        return lambda: balance.collect(plan.id, creds.api_key, creds.base_url)

    if plan.quota_kind == "official_sub":
        # §4.5：M2 起走官方 CLI OAuth 凭据查询；未知 app 类型仍降级 unsupported
        tool = official_sub.tool_for_app_type(plan.app_type)
        if tool is None:
            return lambda: _unsupported(
                plan.id, "官方订阅 · 仅消耗（不支持该 app 的额度查询）"
            )
        return lambda: official_sub.collect(plan.id, tool)

    if plan.quota_kind == "token_plan":
        us = plan.usage_script or {}
        provider = us.get("codingPlanProvider")
        endpoint = store.endpoint_url(plan.id) if store else None
        creds = credentials.resolve(plan, endpoint)
        if not provider:
            provider = _route_by_base_url(creds.base_url or "")
        if provider == "kimi":
            return lambda: kimi.collect(plan.id, creds)
        if provider in ("zhipu", "zhipu_team"):
            # 团队版：base_url 与个人版相同，靠 codingPlanProvider 显式路由（§4.3.2）；
            # 仅 zhipu_team 透传组织/项目 ID（个人版忽略 usage_script 残留 team 字段）
            team_org = team_project = None
            if provider == "zhipu_team":
                team_org = us.get("teamOrganizationId") or us.get("team_organization_id")
                team_project = us.get("teamProjectId") or us.get("team_project_id")
            return lambda: token_plan.collect_zhipu(
                plan.id, creds.api_key, creds.base_url,
                team_org=team_org, team_project=team_project,
            )
        if provider == "minimax":
            return lambda: token_plan.collect_minimax(
                plan.id, creds.api_key, creds.base_url
            )
        if provider == "zenmux":
            return lambda: token_plan.collect_zenmux(
                plan.id, creds.api_key, creds.base_url
            )
        return lambda: _unsupported(
            plan.id, f"token_plan 模板 {provider or '未知'}（M2+ 支持）"
        )

    # custom_script
    if is_opencode_go_native(plan):
        account = config.default_opencode_account_data
        if account and account.ready:
            return lambda: opencode_go.collect(plan.id, account)
        # 凭据未配置：仍尝试 Node 执行库中脚本（桥接在跑时可用）
    us = plan.usage_script or {}
    code = us.get("code")
    if not code:
        return lambda: _unsupported(plan.id, "脚本为空")
    endpoint = store.endpoint_url(plan.id) if store else None
    creds = credentials.resolve(plan, endpoint)
    timeout = int(us.get("timeout") or 10)
    return lambda: custom_script.collect(
        plan.id, code, creds, timeout_sec=timeout, label=plan.name
    )


def _route_by_base_url(base_url: str) -> str | None:
    base_url = base_url.lower()  # 与 balance/token_plan 的判定一致大小写不敏感
    for substr, provider in _BASE_URL_ROUTES:
        if substr in base_url:
            return provider
    return None


def error_to_result(plan_id: str, exc: CollectorError) -> QuotaResult:
    """CollectorError -> ok=False 的 QuotaResult（供调度器统一归一化）。"""
    return QuotaResult(
        plan_id=plan_id,
        ok=False,
        error=exc.code,
        message=exc.message or None,
        fetched_at=int(time.time()),
    )

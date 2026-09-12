"""凭证解析（DESIGN.md §3.3 / §3.4）。

解析顺序与 CC Switch 保持一致：usage_script 显式值 > settings_config 推导值。
解析结果只在内存中用于构造请求，禁止落盘/入日志（§9）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Plan


@dataclass
class Credentials:
    api_key: str | None = None
    base_url: str | None = None
    access_token: str | None = None
    user_id: str | None = None


def _env_of(settings_config: dict) -> dict:
    env = settings_config.get("env")
    return env if isinstance(env, dict) else {}


def resolve(plan: Plan, endpoint_url: str | None = None) -> Credentials:
    """按 app_type 推导 apiKey/baseUrl，再应用 usage_script 显式覆盖。

    endpoint_url: provider_endpoints 表中该 provider 的 url（codex 的兜底来源）。
    """
    sc = plan.settings_config or {}
    api_key: str | None = None
    base_url: str | None = None

    if plan.app_type == "codex":
        auth = sc.get("auth")
        if isinstance(auth, dict):
            api_key = auth.get("OPENAI_API_KEY")
        base_url = endpoint_url
    elif plan.app_type == "gemini":
        env = _env_of(sc)
        api_key = env.get("GEMINI_API_KEY")
        base_url = env.get("GOOGLE_GEMINI_BASE_URL")
    else:
        # claude / claude-desktop 等：第三方走 env，官方登录无 key
        env = _env_of(sc)
        api_key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
        base_url = env.get("ANTHROPIC_BASE_URL")

    us = plan.usage_script or {}
    api_key = us.get("apiKey") or api_key
    base_url = us.get("baseUrl") or base_url
    if isinstance(base_url, str):
        base_url = base_url.rstrip("/") or None

    return Credentials(
        api_key=str(api_key) if api_key else None,
        base_url=base_url,
        access_token=us.get("accessToken"),
        user_id=us.get("userId"),
    )

"""凭证解析测试：§3.3 实测键结构 + §3.4 显式覆盖优先。"""

from __future__ import annotations

from token_widget.credentials import resolve
from token_widget.models import Plan


def _plan(app_type: str, settings_config: dict, usage_script: dict | None = None) -> Plan:
    return Plan(
        id="p", app_type=app_type, name="n", category=None,
        icon=None, icon_color=None, is_current=False,
        quota_kind="custom_script",
        settings_config=settings_config, usage_script=usage_script,
    )


def test_codex_auth_key() -> None:
    plan = _plan("codex", {"auth": {"OPENAI_API_KEY": "sk-x"}, "config": {}})
    creds = resolve(plan, endpoint_url="https://api.example.com/v1/")
    assert creds.api_key == "sk-x"
    assert creds.base_url == "https://api.example.com/v1"  # 尾斜杠去除


def test_gemini_env_keys() -> None:
    plan = _plan("gemini", {"env": {
        "GOOGLE_GEMINI_BASE_URL": "https://dash.example/v1",
        "GEMINI_API_KEY": "sk-g",
        "GEMINI_MODEL": "m",
    }})
    creds = resolve(plan)
    assert creds.api_key == "sk-g"
    assert creds.base_url == "https://dash.example/v1"


def test_claude_third_party_env() -> None:
    plan = _plan("claude", {"env": {
        "ANTHROPIC_AUTH_TOKEN": "sk-ant",
        "ANTHROPIC_BASE_URL": "https://proxy.example.com",
    }})
    creds = resolve(plan)
    assert creds.api_key == "sk-ant"
    assert creds.base_url == "https://proxy.example.com"


def test_claude_official_empty_config() -> None:
    creds = resolve(_plan("claude", {}))
    assert creds.api_key is None and creds.base_url is None


def test_usage_script_explicit_overrides() -> None:
    """显式值 > settings_config 推导值（§3.4）。"""
    plan = _plan(
        "codex",
        {"auth": {"OPENAI_API_KEY": "sk-derived"}},
        {"apiKey": "sk-explicit", "baseUrl": "https://explicit.example.com/"},
    )
    creds = resolve(plan, endpoint_url="https://derived.example.com")
    assert creds.api_key == "sk-explicit"
    assert creds.base_url == "https://explicit.example.com"


def test_access_token_and_user_id_passthrough() -> None:
    plan = _plan("codex", {}, {"accessToken": "tok", "userId": "u1"})
    creds = resolve(plan)
    assert creds.access_token == "tok"
    assert creds.user_id == "u1"


def test_base_url_root_slash_becomes_none() -> None:
    """baseUrl="/" rstrip 后为空串 -> 归 None，不产生空 base_url。"""
    creds = resolve(_plan("codex", {}, {"baseUrl": "/"}))
    assert creds.base_url is None


def test_codex_without_auth_dict() -> None:
    """codex 无 auth dict -> api_key None。"""
    assert resolve(_plan("codex", {})).api_key is None
    assert resolve(_plan("codex", {"auth": "not-a-dict"})).api_key is None


def test_codex_endpoint_url_none() -> None:
    """codex endpoint_url=None -> base_url None（api_key 仍来自 auth）。"""
    plan = _plan("codex", {"auth": {"OPENAI_API_KEY": "sk-x"}})
    creds = resolve(plan, endpoint_url=None)
    assert creds.api_key == "sk-x"
    assert creds.base_url is None

"""挂件自有配置测试：§7 round-trip、损坏回退与原子写降级。"""

from __future__ import annotations

import json

from token_widget.config import (
    ENV_OVERRIDE,
    OpencodeGoAccount,
    WidgetConfig,
    config_path,
    load,
    save,
)


def test_save_load_round_trip(tmp_path) -> None:
    path = tmp_path / "config.json"
    cfg = WidgetConfig(
        selected_plan_ids=["a", "b"],
        window_x=10,
        window_y=-20,
        always_on_top=False,
        refresh_enabled=False,
        opencode_go_accounts={
            "main": OpencodeGoAccount(auth_cookie="c", workspace_id="w"),
        },
        default_opencode_account="main",
    )
    save(cfg, path)
    back = load(path)
    assert back.selected_plan_ids == ["a", "b"]
    assert back.window_x == 10 and back.window_y == -20
    assert back.always_on_top is False
    assert back.refresh_enabled is False
    acc = back.opencode_go_accounts["main"]
    assert acc.auth_cookie == "c" and acc.workspace_id == "w"
    assert back.default_opencode_account == "main"


def test_load_missing_or_corrupt_falls_back_to_default(tmp_path) -> None:
    assert load(tmp_path / "missing.json") == WidgetConfig()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load(bad) == WidgetConfig()


def test_load_truthy_non_dict_fields_do_not_crash(tmp_path) -> None:
    """window/refresh/opencodeGo/accounts 为 truthy 非 dict 时回退默认，不抛 AttributeError。"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({
            "selectedPlanIds": ["a"],
            "window": "abc",
            "refresh": 42,
            "opencodeGo": {"accounts": "xyz", "default": 7},
        }),
        encoding="utf-8",
    )
    cfg = load(path)
    assert cfg.selected_plan_ids == ["a"]
    assert cfg.window_x is None and cfg.always_on_top is True
    assert cfg.refresh_enabled is True
    assert cfg.opencode_go_accounts == {}
    assert cfg.default_opencode_account == "main"


def test_env_override_path(tmp_path, monkeypatch) -> None:
    """TOKEN_WIDGET_CONFIG 覆盖默认路径，load/save 都走该路径。"""
    custom = tmp_path / "custom.json"
    monkeypatch.setenv(ENV_OVERRIDE, str(custom))
    assert config_path() == custom
    save(WidgetConfig(selected_plan_ids=["x"]))
    assert load().selected_plan_ids == ["x"]
    assert json.loads(custom.read_text(encoding="utf-8"))["selectedPlanIds"] == ["x"]


def test_opencode_account_ready() -> None:
    assert not OpencodeGoAccount().ready
    assert not OpencodeGoAccount(auth_cookie="c").ready
    assert not OpencodeGoAccount(workspace_id="w").ready
    assert OpencodeGoAccount(auth_cookie="c", workspace_id="w").ready


def test_default_opencode_account_data() -> None:
    cfg = WidgetConfig()
    assert cfg.default_opencode_account_data is None
    acc = OpencodeGoAccount(auth_cookie="c", workspace_id="w")
    cfg.opencode_go_accounts["main"] = acc
    assert cfg.default_opencode_account_data is acc
    cfg.default_opencode_account = "ghost"
    assert cfg.default_opencode_account_data is None


def test_save_leaves_no_tmp_residue(tmp_path) -> None:
    path = tmp_path / "config.json"
    save(WidgetConfig(), path)
    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []

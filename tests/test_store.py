"""Store 层测试：§10 临时 SQLite + 降级行为。"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from token_widget.store import DbAccessError, Store


def test_probe_and_load_seven_plans(sample_db) -> None:
    store = Store(sample_db)
    caps = store.probe()
    assert caps.providers_ok and caps.rollups_ok and caps.missing == []

    plans = store.load_plans(selected_ids={"kimi-id"})
    assert len(plans) == 7
    by_id = {p.id: p for p in plans}

    assert by_id["kimi-id"].quota_kind == "token_plan"
    assert by_id["kimi-id"].selected is True
    assert by_id["kimi-id"].auto_query_interval_min == 5
    assert by_id["opencode-id"].quota_kind == "custom_script"
    assert by_id["claude-official"].quota_kind == "official_sub"
    assert by_id["codex-official"].quota_kind == "official_sub"
    assert by_id["bailian-id"].quota_kind == "consumption_only"
    assert by_id["claude-official"].is_current is True

    # settings_config 解析到内存（密钥不落盘、不入日志由调用方保证）
    assert by_id["kimi-id"].settings_config["auth"]["OPENAI_API_KEY"] == "sk-fake-kimi"


def test_consumption_summary(sample_db) -> None:
    con = sqlite3.connect(sample_db)
    con.executemany(
        "INSERT INTO usage_daily_rollups (date, app_type, provider_id, model,"
        " input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,"
        " total_cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("2026-08-03", "codex", "kimi-id", "k2", 100, 50, 10, 5, 0.5),
            ("2026-08-03", "codex", "kimi-id", "k2", 200, 100, 0, 0, 1.0),
            ("2026-08-03", "claude", "_session", "opus", 10, 10, 0, 0, 0.2),
            ("2026-08-02", "codex", "kimi-id", "k2", 999, 0, 0, 0, 9.9),
        ],
    )
    con.commit()
    con.close()

    store = Store(sample_db)
    store.probe()
    summary = store.consumption("2026-08-03")
    assert summary is not None
    assert abs(summary.total_cost_usd - 1.7) < 1e-9
    assert summary.total_tokens == 485  # kimi 465 + _session 20
    by_plan = {e.plan_id: e for e in summary.by_plan}
    assert by_plan["kimi-id"].tokens == 465
    assert set(by_plan["kimi-id"].models) == {"k2"}
    assert "_session" in by_plan

    # 按模型拆解（M2 行详情面板）
    assert len(by_plan["kimi-id"].by_model) == 1
    model_entry = by_plan["kimi-id"].by_model[0]
    assert model_entry.model == "k2"
    assert abs(model_entry.cost_usd - 1.5) < 1e-9
    assert model_entry.tokens == 465


def test_consumption_by_model_breakdown(sample_db) -> None:
    con = sqlite3.connect(sample_db)
    con.executemany(
        "INSERT INTO usage_daily_rollups (date, app_type, provider_id, model,"
        " input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,"
        " total_cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("2026-08-03", "codex", "kimi-id", "k2", 100, 0, 0, 0, 1.0),
            ("2026-08-03", "codex", "kimi-id", "k1", 50, 0, 0, 0, 3.0),
            ("2026-08-03", "codex", "kimi-id", "k2", 10, 0, 0, 0, 0.5),
        ],
    )
    con.commit()
    con.close()

    store = Store(sample_db)
    store.probe()
    entry = store.consumption("2026-08-03").by_plan[0]
    assert entry.plan_id == "kimi-id"
    assert abs(entry.cost_usd - 4.5) < 1e-9
    assert entry.tokens == 160
    # 按 cost 降序：k1 (3.0) 在前，k2 聚合为 1.5
    assert [m.model for m in entry.by_model] == ["k1", "k2"]
    assert abs(entry.by_model[0].cost_usd - 3.0) < 1e-9
    assert entry.by_model[1].tokens == 110
    assert set(entry.models) == {"k1", "k2"}


def test_missing_tables_degrade_not_crash(tmp_path) -> None:
    """§10 DB 降级：空库 -> 探测失败、功能降级返回空，不崩溃。"""
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()
    store = Store(db_path)
    caps = store.probe()
    assert not caps.providers_ok and not caps.rollups_ok
    assert store.load_plans(set()) == []
    assert store.consumption("2026-08-03") is None


def test_missing_column_degrades(tmp_path) -> None:
    """缺列（无 meta）-> providers 功能降级。"""
    db_path = tmp_path / "partial.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT)")
    con.commit()
    con.close()
    store = Store(db_path)
    caps = store.probe()
    assert not caps.providers_ok
    assert any(m.startswith("providers.") for m in caps.missing)
    assert store.load_plans(set()) == []


def test_endpoint_url_lookup(sample_db) -> None:
    con = sqlite3.connect(sample_db)
    con.execute(
        "CREATE TABLE provider_endpoints ("
        "id INTEGER PRIMARY KEY, provider_id TEXT NOT NULL,"
        "app_type TEXT NOT NULL, url TEXT NOT NULL, added_at INTEGER)"
    )
    con.execute(
        "INSERT INTO provider_endpoints (provider_id, app_type, url, added_at)"
        " VALUES ('kimi-id', 'codex', 'https://api.kimi.com/coding/v1', 1)"
    )
    con.commit()
    con.close()

    store = Store(sample_db)
    assert store.endpoint_url("kimi-id") == "https://api.kimi.com/coding/v1"
    assert store.endpoint_url("missing") is None


def test_mtime_tracks_wal_file(tmp_path) -> None:
    """WAL 模式：变更只写 -wal 时 mtime 也要能感知（主文件可能不变）。"""
    db = tmp_path / "cc-switch.db"
    db.write_bytes(b"db")
    store = Store(db)
    base = store.mtime()
    assert base > 0
    wal = tmp_path / "cc-switch.db-wal"
    wal.write_bytes(b"wal")
    os.utime(wal, (base + 100, base + 100))
    assert store.mtime() == base + 100


def test_probe_quick(sample_db) -> None:
    """quick 探测（UI 轮询用）在正常 DB 上与常规探测结果一致。"""
    caps = Store(sample_db).probe(quick=True)
    assert caps.providers_ok and caps.rollups_ok and caps.missing == []


def test_consumption_quick_fails_fast_when_db_locked(sample_db) -> None:
    """quick=True（UI 线程轮询）：DB 被独占写锁时快速失败，不卡 UI 线程约 3 秒。"""
    store = Store(sample_db)
    store.probe()
    holder = sqlite3.connect(sample_db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute(
        "INSERT INTO usage_daily_rollups (date, app_type, provider_id)"
        " VALUES ('2026-08-03', 'codex', 'kimi-id')"
    )
    try:
        start = time.monotonic()
        with pytest.raises(DbAccessError):
            store.consumption("2026-08-03", quick=True)
        assert time.monotonic() - start < 1.5
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_probe_db_error_preserves_capabilities(sample_db, monkeypatch) -> None:
    """瞬时锁库探测失败：保留已知良好的 capabilities（避免 load_plans 清空、UI 行消失）。"""
    store = Store(sample_db)
    good = store.probe()
    assert good.providers_ok and good.rollups_ok and good.missing == []

    def _raise(*args, **kwargs):
        raise DbAccessError("locked")

    monkeypatch.setattr(store, "_connect", _raise)
    caps = store.probe()
    assert caps.providers_ok and caps.rollups_ok and caps.missing == []

    # 首次探测本来就是全 False：锁库失败时同样全 False（功能降级）
    fresh = Store(sample_db)
    monkeypatch.setattr(fresh, "_connect", _raise)
    caps = fresh.probe()
    assert not caps.providers_ok and not caps.rollups_ok


def test_endpoint_url_missing_table_returns_none(sample_db) -> None:
    """provider_endpoints 表不存在（旧版库）：防御性返回 None。"""
    assert Store(sample_db).endpoint_url("kimi-id") is None

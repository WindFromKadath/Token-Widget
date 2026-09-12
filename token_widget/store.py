"""CC Switch 数据库只读访问（DESIGN.md §3）。

- URI `file:<path>?mode=ro` 只读打开，busy 重试 3 次
- Schema 漂移防御：按列名读取，缺表/缺列时功能降级不崩溃
- settings_config 含密钥：只在内存使用，禁止入日志
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .models import (
    ConsumptionByPlan,
    ConsumptionSummary,
    ModelConsumption,
    Plan,
    classify_quota_kind,
)

OPEN_RETRIES = 3
OPEN_RETRY_DELAY = 0.2

REQUIRED_PROVIDER_COLUMNS = {
    "id", "app_type", "name", "category", "icon", "icon_color",
    "is_current", "settings_config", "meta", "sort_index",
}
REQUIRED_ROLLUP_COLUMNS = {
    "date", "provider_id", "model", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "total_cost_usd",
}


class DbAccessError(Exception):
    """cc-switch.db 暂时不可读（对应 error code: db_locked）。"""


@dataclass
class Capabilities:
    providers_ok: bool
    rollups_ok: bool
    missing: list[str]


def default_db_path() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / ".cc-switch" / "cc-switch.db"


class Store:
    """短连接、只读；每次操作独立开连接，避免与 CC Switch 写入互相干扰。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.capabilities = Capabilities(False, False, [])
        # provider_endpoints 列名探测结果的一次性缓存（每次采集都调用 endpoint_url，
        # 避免重复 PRAGMA；列缺失/锁库的防御性 None 返回不受影响）
        self._endpoint_cols: set[str] | None = None

    # ------------------------------------------------------------ 连接

    def _connect(
        self, *, timeout: float = 3.0, retries: int = OPEN_RETRIES
    ) -> sqlite3.Connection:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                con = sqlite3.connect(
                    f"file:{self.path}?mode=ro", uri=True, timeout=timeout
                )
                con.row_factory = sqlite3.Row
                return con
            except sqlite3.OperationalError as exc:
                last = exc
                if attempt < retries - 1:  # 最后一次失败后不再白等
                    time.sleep(OPEN_RETRY_DELAY)
        raise DbAccessError(f"无法只读打开 {self.path}: {last}")

    def _connect_quick(self) -> sqlite3.Connection:
        """短超时、不重试：供 UI 线程轮询用，DB 被写锁时快速失败下轮再试。"""
        return self._connect(timeout=0.3, retries=1)

    def mtime(self) -> float:
        """主文件与 -wal 取较新 mtime（WAL 模式下变更可能只落在 -wal 上）。"""
        latest = 0.0
        for candidate in (self.path, Path(f"{self.path}-wal")):
            try:
                latest = max(latest, candidate.stat().st_mtime)
            except OSError:
                pass
        return latest

    # ------------------------------------------------------------ 探测

    def probe(self, *, quick: bool = False) -> Capabilities:
        """启动时探测所需表/列是否齐全（§3.1 Schema 漂移防御）。

        quick=True：短超时、不重试（UI 线程轮询用，锁住时本轮放弃）。
        """
        missing: list[str] = []
        providers_ok = rollups_ok = False
        try:
            with closing(self._connect_quick() if quick else self._connect()) as con:
                tables = {
                    r[0]
                    for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "providers" in tables:
                    cols = {
                        c[1] for c in con.execute("PRAGMA table_info(providers)")
                    }
                    lack = REQUIRED_PROVIDER_COLUMNS - cols
                    providers_ok = not lack
                    missing.extend(f"providers.{c}" for c in sorted(lack))
                else:
                    missing.append("providers")

                if "usage_daily_rollups" in tables:
                    cols = {
                        c[1]
                        for c in con.execute(
                            "PRAGMA table_info(usage_daily_rollups)"
                        )
                    }
                    lack = REQUIRED_ROLLUP_COLUMNS - cols
                    rollups_ok = not lack
                    missing.extend(f"usage_daily_rollups.{c}" for c in sorted(lack))
                else:
                    missing.append("usage_daily_rollups")
        except DbAccessError:
            # 瞬时锁库：保留已知良好的 capabilities，避免 load_plans 清空、UI 行消失
            return self.capabilities
        self.capabilities = Capabilities(providers_ok, rollups_ok, missing)
        return self.capabilities

    # ------------------------------------------------------------ Plan 清单

    def load_plans(self, selected_ids: set[str], *, quick: bool = False) -> list[Plan]:
        if not self.capabilities.providers_ok:
            self.probe(quick=quick)
        if not self.capabilities.providers_ok:
            return []
        rows = self._query_providers(quick=quick)
        plans: list[Plan] = []
        for row in rows:
            usage_script = _parse_json(row["meta"], "usage_script")
            plans.append(
                Plan(
                    id=str(row["id"]),
                    app_type=str(row["app_type"] or ""),
                    name=str(row["name"] or ""),
                    category=row["category"],
                    icon=row["icon"],
                    icon_color=row["icon_color"],
                    is_current=bool(row["is_current"]),
                    quota_kind=classify_quota_kind(row["category"], usage_script),
                    selected=str(row["id"]) in selected_ids,
                    auto_query_interval_min=_auto_interval(usage_script),
                    settings_config=_parse_json(row["settings_config"]) or {},
                    usage_script=usage_script,
                )
            )
        return plans

    def _query_providers(self, *, quick: bool = False) -> list[sqlite3.Row]:
        sql = (
            "SELECT id, app_type, name, category, icon, icon_color, is_current,"
            " settings_config, meta FROM providers"
            " ORDER BY app_type, sort_index"
        )
        try:
            with closing(self._connect_quick() if quick else self._connect()) as con:
                return list(con.execute(sql))
        except sqlite3.OperationalError as exc:
            raise DbAccessError(str(exc)) from exc

    # ------------------------------------------------------------ endpoints

    def endpoint_url(self, provider_id: str) -> str | None:
        """provider_endpoints 表中该 provider 的 url（codex base_url 兜底，防御性访问）。"""
        try:
            # UI 线程同步调用：短超时连接，锁库时快速走防御性 None 返回
            with closing(self._connect_quick()) as con:
                cols = self._endpoint_cols
                if cols is None:
                    cols = {
                        c[1]
                        for c in con.execute("PRAGMA table_info(provider_endpoints)")
                    }
                    if cols:
                        # 仅缓存非空结果：表尚不存在（返回空集）时不缓存，
                        # CC Switch 后续迁移建表仍可被探测到
                        self._endpoint_cols = cols
                if not {"provider_id", "url"} <= cols:
                    return None
                row = con.execute(
                    "SELECT url FROM provider_endpoints"
                    " WHERE provider_id = ? ORDER BY added_at DESC LIMIT 1",
                    (provider_id,),
                ).fetchone()
                return row["url"] if row else None
        except (sqlite3.OperationalError, DbAccessError):
            return None

    # ------------------------------------------------------------ 当日消耗

    def consumption(self, date_str: str, *, quick: bool = False) -> ConsumptionSummary | None:
        """§3.5 当日汇总（含按模型拆解）；表缺失或无数据返回 None / 空摘要。"""
        if not self.capabilities.rollups_ok:
            self.probe(quick=quick)
        if not self.capabilities.rollups_ok:
            return None
        sql = (
            "SELECT provider_id, model,"
            "       SUM(total_cost_usd) AS cost,"
            "       SUM(input_tokens + output_tokens"
            "           + cache_read_tokens + cache_creation_tokens) AS tokens"
            " FROM usage_daily_rollups WHERE date = ?"
            " GROUP BY provider_id, model ORDER BY cost DESC"
        )
        summary = ConsumptionSummary(date=date_str, total_cost_usd=0.0, total_tokens=0)
        try:
            with closing(self._connect_quick() if quick else self._connect()) as con:
                rows = list(con.execute(sql, (date_str,)))
        except sqlite3.OperationalError as exc:
            raise DbAccessError(str(exc)) from exc
        by_plan: dict[str, ConsumptionByPlan] = {}
        for row in rows:
            plan_id = str(row["provider_id"])
            entry = by_plan.get(plan_id)
            if entry is None:
                entry = ConsumptionByPlan(plan_id=plan_id, cost_usd=0.0, tokens=0)
                by_plan[plan_id] = entry
            model = str(row["model"] or "")
            cost = float(row["cost"] or 0.0)
            tokens = int(row["tokens"] or 0)
            entry.cost_usd += cost
            entry.tokens += tokens
            if model:
                entry.by_model.append(
                    ModelConsumption(model=model, cost_usd=cost, tokens=tokens)
                )
                if model not in entry.models:
                    entry.models.append(model)
            summary.total_cost_usd += cost
            summary.total_tokens += tokens
        summary.by_plan = list(by_plan.values())
        return summary


# ------------------------------------------------------------ 工具

def _parse_json(raw, key: str | None = None):
    if not raw:
        return None if key else {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None if key else {}
    if key is not None:
        if not isinstance(parsed, dict):
            return None
        value = parsed.get(key)
        return value if isinstance(value, dict) else None
    return parsed if isinstance(parsed, dict) else {}


def _auto_interval(usage_script: dict | None) -> int:
    value = (usage_script or {}).get("autoQueryInterval", 5)
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 5

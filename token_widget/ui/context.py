"""应用上下文：共享状态、采集分发、DB 变更跟随。

stale 回退（§4.4 / §6.1）：plan 失败时展示上次成功数据并置 stale。
"""

from __future__ import annotations

import time
from dataclasses import replace

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .. import collectors, config as config_mod
from ..collectors.base import CollectorError
from ..config import WidgetConfig
from ..models import ConsumptionSummary, Plan, QuotaResult
from ..scheduler import RefreshScheduler
from ..store import DbAccessError, Store, default_db_path


class AppSignals(QObject):
    quota_updated = Signal(str, object)      # plan_id, QuotaResult（展示用）
    plans_changed = Signal()                 # DB 变更 / 勾选变化
    consumption_updated = Signal(object)     # ConsumptionSummary | None


class AppContext:
    def __init__(self, store: Store | None = None) -> None:
        self.config: WidgetConfig = config_mod.load()
        self.store = store or Store(default_db_path())
        self.store.probe()
        self.signals = AppSignals()
        self.scheduler = RefreshScheduler()
        self.plans: list[Plan] = []
        self._last_success: dict[str, QuotaResult] = {}
        self._display: dict[str, QuotaResult] = {}  # 行渲染缓存（重建行后复用）
        self._db_mtime = 0.0
        self._shutdown = False

    def begin_shutdown(self) -> None:
        """退出流程：在途 Worker 完成后不再广播信号（防已删除信号源竞态）。"""
        self._shutdown = True

    # ------------------------------------------------------------ Plan 清单

    def reload_plans(self, *, quick: bool = False) -> None:
        selected = set(self.config.selected_plan_ids)
        # 先记 mtime 再读：读写间隙内 CC Switch 的写入由下一轮轮询兜底，
        # 避免出现「mtime 已更新但数据未含该变更」而被跳过
        mtime = self.store.mtime()
        try:
            self.plans = self.store.load_plans(selected, quick=quick)
        except DbAccessError:
            return  # 保持旧清单，等待下次 mtime 轮询
        self.scheduler.sync_plans([p for p in self.plans if p.selected])
        self._db_mtime = mtime
        self.signals.plans_changed.emit()

    def db_changed(self) -> bool:
        mtime = self.store.mtime()
        return mtime != 0.0 and mtime != self._db_mtime

    # ------------------------------------------------------------ 消耗

    def refresh_consumption(self, *, quick: bool = False) -> ConsumptionSummary | None:
        try:
            summary = self.store.consumption(time.strftime("%Y-%m-%d"), quick=quick)
        except DbAccessError:
            return None
        self.signals.consumption_updated.emit(summary)
        return summary

    # ------------------------------------------------------------ 采集分发

    def collector_for(self, plan: Plan):
        return collectors.build_collector(plan, self.config, self.store)

    def dispatch_due(self) -> None:
        for plan_id in self.scheduler.due_plan_ids():
            plan = next((p for p in self.plans if p.id == plan_id), None)
            if plan is None:
                continue
            fn = self.collector_for(plan)
            if fn is None:
                # consumption_only：不请求网络，仅展示消耗（§4.6）
                self.scheduler.mark_static(plan_id)
                display = QuotaResult(
                    plan_id=plan_id, ok=False,
                    error="unsupported", message="仅消耗",
                    fetched_at=int(time.time()),
                )
                self._display[plan_id] = display
                self.signals.quota_updated.emit(plan_id, display)
                continue
            self.scheduler.mark_started(plan_id)
            Worker(plan_id, fn, self).start()

    def run_collector(self, plan_id: str, fn) -> QuotaResult | None:
        """执行采集并把结果（含 stale 回退）广播给 UI；shutdown 时返回 None。"""
        try:
            result = fn()
        except CollectorError as exc:
            result = collectors.error_to_result(plan_id, exc)
        except Exception as exc:  # 兜底：单个 plan 失败不影响其他 plan（P5）
            result = collectors.error_to_result(
                plan_id, CollectorError("fetch_failed", str(exc)[:120])
            )
        self.scheduler.mark_result(plan_id, result)
        if self._shutdown:
            return
        display = result
        if result.ok:
            self._last_success[plan_id] = result
        else:
            last = self._last_success.get(plan_id)
            if last is not None and result.error in ("fetch_failed", "db_locked"):
                display = replace(last, stale=True)
        self._display[plan_id] = display
        self.signals.quota_updated.emit(plan_id, display)
        return result

    def display_result(self, plan_id: str) -> QuotaResult | None:
        return self._display.get(plan_id)


class Worker(QRunnable):
    """后台执行单个 plan 的采集器；结果经 AppContext 归一化后广播。"""

    def __init__(self, plan_id: str, fn, ctx: AppContext) -> None:
        super().__init__()
        self._plan_id = plan_id
        self._fn = fn
        self._ctx = ctx

    @Slot()
    def run(self) -> None:
        self._ctx.run_collector(self._plan_id, self._fn)

    def start(self) -> None:
        from PySide6.QtCore import QThreadPool

        QThreadPool.globalInstance().start(self)

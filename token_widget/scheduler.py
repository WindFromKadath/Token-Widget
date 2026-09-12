"""刷新调度器（DESIGN.md §5）。

规则：
- 每 plan 独立计时，基础间隔 = autoQueryInterval（分钟，0 = 不自动刷）
- 近限升频：任一窗口 usedPercent >= 90 -> 间隔 1 分钟
- 失败退避：间隔 x2，上限 30 分钟；成功后恢复
- 单飞：同一 plan 并发只许一个在途请求
- 手动刷新不受间隔限制，但遵守单飞

纯逻辑、时钟可注入，便于假时钟测试（§10）。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .models import Plan, QuotaResult

MAX_BACKOFF_MIN = 30.0
NEAR_LIMIT_PERCENT = 90.0
NEAR_LIMIT_INTERVAL_MIN = 1.0
STARTUP_STAGGER_SEC = 3.0


@dataclass
class PlanRuntime:
    plan_id: str
    base_interval_min: int
    next_due: float = 0.0
    in_flight: bool = False
    fail_streak: int = 0
    last_result: QuotaResult | None = None


class RefreshScheduler:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._runtimes: dict[str, PlanRuntime] = {}
        self._paused = False

    # ------------------------------------------------------------ 状态

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def runtime(self, plan_id: str) -> PlanRuntime | None:
        return self._runtimes.get(plan_id)

    def last_result(self, plan_id: str) -> QuotaResult | None:
        rt = self._runtimes.get(plan_id)
        return rt.last_result if rt else None

    # ------------------------------------------------------------ 同步

    def sync_plans(self, plans: list[Plan], stagger: float = STARTUP_STAGGER_SEC) -> list[str]:
        """DB 变更/勾选变化后同步：新增立即首刷（错峰），消失移除。

        返回新增的 plan_id 列表。
        """
        now = self._clock()
        seen: set[str] = set()
        added: list[str] = []
        for plan in plans:
            seen.add(plan.id)
            rt = self._runtimes.get(plan.id)
            if rt is None:
                delay = random.uniform(0.0, stagger) if stagger > 0 else 0.0
                self._runtimes[plan.id] = PlanRuntime(
                    plan_id=plan.id,
                    base_interval_min=plan.auto_query_interval_min,
                    next_due=now + delay,
                )
                added.append(plan.id)
            else:
                rt.base_interval_min = plan.auto_query_interval_min
                if (
                    rt.next_due == float("inf")
                    and not rt.in_flight
                    and plan.auto_query_interval_min > 0
                ):
                    # 曾被 mark_static / 间隔 0 置为永不到期：现间隔 >0，复活调度
                    rt.next_due = now
        for plan_id in list(self._runtimes):
            if plan_id not in seen:
                del self._runtimes[plan_id]
        return added

    # ------------------------------------------------------------ 调度

    def due_plan_ids(self) -> list[str]:
        """到点且未在途、未暂停的 plan（含手动请求；间隔 0 由 mark_result 置 inf）。"""
        if self._paused:
            return []
        now = self._clock()
        return [
            plan_id
            for plan_id, rt in self._runtimes.items()
            if not rt.in_flight and rt.next_due <= now
        ]

    def request_refresh(self, plan_id: str) -> bool:
        """手动刷新：立即到期（遵守单飞）；返回是否可执行。"""
        rt = self._runtimes.get(plan_id)
        if rt is None or rt.in_flight:
            return False
        rt.next_due = self._clock()
        return True

    def request_refresh_all(self) -> None:
        for rt in self._runtimes.values():
            if not rt.in_flight:
                rt.next_due = self._clock()

    def mark_started(self, plan_id: str) -> None:
        rt = self._runtimes.get(plan_id)
        if rt:
            rt.in_flight = True

    def mark_static(self, plan_id: str) -> None:
        """无需采集的 plan（consumption_only）：不再进入调度。"""
        rt = self._runtimes.get(plan_id)
        if rt:
            rt.next_due = float("inf")

    def mark_result(self, plan_id: str, result: QuotaResult) -> None:
        """成功：清零失败计数并按近限情况定间隔；失败：退避 x2 上限 30 分钟。"""
        rt = self._runtimes.get(plan_id)
        if rt is None:
            return
        rt.in_flight = False
        rt.last_result = result
        now = self._clock()
        if result.ok:
            rt.fail_streak = 0
        else:
            rt.fail_streak += 1
        interval = self._effective_interval_min(rt, result)
        if interval > 0:
            rt.next_due = now + interval * 60.0
        else:
            rt.next_due = float("inf")

    # ------------------------------------------------------------ 间隔

    def _effective_interval_min(self, rt: PlanRuntime, result: QuotaResult) -> float:
        base = float(rt.base_interval_min)
        if base <= 0:
            return 0.0
        if result.ok:
            worst = result.worst
            if worst is not None and worst.used_percent >= NEAR_LIMIT_PERCENT:
                return NEAR_LIMIT_INTERVAL_MIN
            return base
        # 失败退避：base * 2^streak，上限 30 分钟
        return min(base * (2 ** rt.fail_streak), MAX_BACKOFF_MIN)

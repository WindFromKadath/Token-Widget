"""调度器测试：假时钟验证升频/退避/单飞（§10）。"""

from __future__ import annotations

from token_widget.models import Plan, QuotaResult, QuotaWindow
from token_widget.scheduler import RefreshScheduler


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _plan(plan_id: str = "p1", interval: int = 5) -> Plan:
    return Plan(
        id=plan_id, app_type="codex", name=plan_id, category=None,
        icon=None, icon_color=None, is_current=False,
        quota_kind="custom_script", auto_query_interval_min=interval,
    )


def _ok_result(plan_id: str = "p1", percent: float = 10.0) -> QuotaResult:
    return QuotaResult(
        plan_id=plan_id, ok=True, fetched_at=0,
        windows=[QuotaWindow(key="5h", label="5小时", used_percent=percent)],
    )


def _fail_result(plan_id: str = "p1") -> QuotaResult:
    return QuotaResult(plan_id=plan_id, ok=False, error="fetch_failed")


def test_new_plan_due_immediately_then_base_interval() -> None:
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    added = sched.sync_plans([_plan()], stagger=0)
    assert added == ["p1"]
    assert sched.due_plan_ids() == ["p1"]

    sched.mark_started("p1")
    sched.mark_result("p1", _ok_result())
    assert sched.due_plan_ids() == []
    clock.advance(4 * 60)
    assert sched.due_plan_ids() == []
    clock.advance(1 * 60)
    assert sched.due_plan_ids() == ["p1"]


def test_near_limit_escalates_to_one_minute() -> None:
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan(interval=5)], stagger=0)
    sched.mark_started("p1")
    sched.mark_result("p1", _ok_result(percent=95.0))
    clock.advance(59)
    assert sched.due_plan_ids() == []
    clock.advance(1)
    assert sched.due_plan_ids() == ["p1"]


def test_failure_backoff_doubles_then_caps() -> None:
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan(interval=5)], stagger=0)

    expected_min = [10, 20, 30, 30]  # base*2^n，上限 30
    for i, expect in enumerate(expected_min):
        assert sched.due_plan_ids() == ["p1"], f"第 {i} 次失败前应到期"
        sched.mark_started("p1")
        sched.mark_result("p1", _fail_result())
        clock.advance(expect * 60 - 1)
        assert sched.due_plan_ids() == []
        clock.advance(1)

    # 成功后恢复基础间隔
    sched.mark_started("p1")
    sched.mark_result("p1", _ok_result())
    clock.advance(4 * 60)
    assert sched.due_plan_ids() == []
    clock.advance(1 * 60)
    assert sched.due_plan_ids() == ["p1"]


def test_single_flight() -> None:
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan(interval=1)], stagger=0)
    assert sched.due_plan_ids() == ["p1"]
    sched.mark_started("p1")
    clock.advance(10 * 60)  # 在途期间即使到期也不重复派发
    assert sched.due_plan_ids() == []


def test_zero_interval_no_auto_refresh_but_manual_and_startup_ok() -> None:
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan(interval=0)], stagger=0)
    # 启动首刷仍会触发一次（§5 启动规则）
    assert sched.due_plan_ids() == ["p1"]
    sched.mark_started("p1")
    sched.mark_result("p1", _ok_result())
    # 之后不再自动到期（0 = 不自动刷）
    clock.advance(24 * 3600)
    assert sched.due_plan_ids() == []
    # 手动刷新不受间隔限制（§5）
    assert sched.request_refresh("p1")
    assert sched.due_plan_ids() == ["p1"]


def test_manual_refresh_bypasses_interval() -> None:
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan(interval=30)], stagger=0)
    sched.mark_started("p1")
    sched.mark_result("p1", _ok_result())
    assert sched.due_plan_ids() == []
    sched.request_refresh("p1")
    assert sched.due_plan_ids() == ["p1"]
    # 单飞：在途时手动刷新被拒
    sched.mark_started("p1")
    assert not sched.request_refresh("p1")


def test_pause_and_sync_removal() -> None:
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan("p1"), _plan("p2")], stagger=0)
    sched.set_paused(True)
    assert sched.due_plan_ids() == []
    sched.set_paused(False)
    assert set(sched.due_plan_ids()) == {"p1", "p2"}

    # DB 变更：p2 消失后被移除
    sched.sync_plans([_plan("p1")], stagger=0)
    assert sched.runtime("p2") is None


def test_mark_static_never_due_again() -> None:
    """consumption_only：mark_static 置 inf 后不再到期。"""
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan(interval=5)], stagger=0)
    sched.mark_static("p1")
    clock.advance(30 * 24 * 3600)
    assert sched.due_plan_ids() == []


def test_sync_plans_revives_inf_next_due() -> None:
    """间隔 0 -> 5：曾被 mark_result 置 inf 的 plan 恢复调度（不再静默失效）。"""
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan(interval=0)], stagger=0)
    sched.mark_started("p1")
    sched.mark_result("p1", _ok_result())  # 间隔 0 -> next_due = inf
    clock.advance(24 * 3600)
    assert sched.due_plan_ids() == []

    added = sched.sync_plans([_plan(interval=5)], stagger=0)
    assert added == []
    assert sched.runtime("p1").base_interval_min == 5
    assert sched.due_plan_ids() == ["p1"]  # 复活为立即到期


def test_sync_plans_updates_existing_base_interval() -> None:
    """已存在 plan：base_interval_min 跟随 DB 变更。"""
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan(interval=5)], stagger=0)
    assert sched.runtime("p1").base_interval_min == 5
    added = sched.sync_plans([_plan(interval=15)], stagger=0)
    assert added == []
    assert sched.runtime("p1").base_interval_min == 15


def test_request_refresh_all() -> None:
    """全局手动刷新：非在途 plan 全部立即到期；在途不受影响。"""
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    sched.sync_plans([_plan("p1"), _plan("p2")], stagger=0)
    sched.mark_started("p1")
    sched.mark_result("p1", _ok_result())
    sched.mark_started("p2")
    sched.mark_result("p2", _ok_result())
    assert sched.due_plan_ids() == []

    sched.mark_started("p1")  # p1 在途
    sched.request_refresh_all()
    assert sched.due_plan_ids() == ["p2"]


def test_last_result() -> None:
    clock = FakeClock()
    sched = RefreshScheduler(clock)
    assert sched.last_result("unknown") is None
    sched.sync_plans([_plan()], stagger=0)
    assert sched.last_result("p1") is None
    result = _ok_result(percent=42.0)
    sched.mark_started("p1")
    sched.mark_result("p1", result)
    assert sched.last_result("p1") is result

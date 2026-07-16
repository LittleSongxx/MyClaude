"""RunContext 单元测试。

RunContext 是「一次 run 的 deadline / 取消 / 后台任务 / 背压」的单一事实来源，
被设计成不依赖 Agent、可独立测试的领域对象——这些测试正是它可独立验证的证据。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from myclaude.run_context import DEFAULT_MAX_CONCURRENCY, RunContext


class TestDeadline:
    def test_no_deadline_returns_none(self) -> None:
        ctx = RunContext.from_wall_time(0)
        assert ctx.deadline is None
        assert ctx.remaining() is None
        assert ctx.expired() is False

    def test_negative_wall_time_means_no_deadline(self) -> None:
        ctx = RunContext.from_wall_time(-5)
        assert ctx.deadline is None
        assert ctx.remaining() is None

    def test_positive_wall_time_sets_deadline(self) -> None:
        ctx = RunContext.from_wall_time(10)
        remaining = ctx.remaining()
        assert remaining is not None
        # 刚创建，剩余应接近 10（留出执行抖动余量）。
        assert 9.0 < remaining <= 10.0
        assert ctx.expired() is False

    def test_expired_when_deadline_passed(self) -> None:
        ctx = RunContext(deadline=time.monotonic() - 1.0)
        assert ctx.remaining() == 0.0
        assert ctx.expired() is True


class TestSleep:
    @pytest.mark.asyncio
    async def test_sleep_without_deadline_completes(self) -> None:
        ctx = RunContext.from_wall_time(0)
        ok = await ctx.sleep(0.01)
        assert ok is True

    @pytest.mark.asyncio
    async def test_sleep_capped_by_deadline_returns_false(self) -> None:
        # 剩余预算 ~0.02s，却请求睡 5s：必须被截断，且不能真的睡 5s。
        ctx = RunContext.from_wall_time(0.02)
        start = time.monotonic()
        ok = await ctx.sleep(5.0)
        elapsed = time.monotonic() - start
        assert ok is False  # 告诉调用方：别再重试了
        assert elapsed < 1.0  # 绝没有睡满 5s——这正是 retry backoff 不越界的保证

    @pytest.mark.asyncio
    async def test_sleep_within_budget_returns_true(self) -> None:
        ctx = RunContext.from_wall_time(10)
        ok = await ctx.sleep(0.01)
        assert ok is True

    @pytest.mark.asyncio
    async def test_sleep_when_already_expired_returns_false_immediately(self) -> None:
        ctx = RunContext(deadline=time.monotonic() - 1.0)
        start = time.monotonic()
        ok = await ctx.sleep(5.0)
        assert ok is False
        assert time.monotonic() - start < 0.5


class TestTimeoutScope:
    @pytest.mark.asyncio
    async def test_no_deadline_does_not_interrupt(self) -> None:
        ctx = RunContext.from_wall_time(0)
        async with ctx.timeout_scope():
            await asyncio.sleep(0.01)
        # 未抛异常即通过。

    @pytest.mark.asyncio
    async def test_scope_times_out_long_await(self) -> None:
        ctx = RunContext.from_wall_time(0.02)
        with pytest.raises(TimeoutError):
            async with ctx.timeout_scope():
                await asyncio.sleep(5.0)

    @pytest.mark.asyncio
    async def test_expired_scope_times_out_immediately(self) -> None:
        ctx = RunContext(deadline=time.monotonic() - 1.0)
        with pytest.raises(TimeoutError):
            async with ctx.timeout_scope():
                await asyncio.sleep(0.01)


class TestTaskRegistry:
    @pytest.mark.asyncio
    async def test_register_and_drain_completes_tasks(self) -> None:
        ctx = RunContext.from_wall_time(0)
        done_flag = {"ran": False}

        async def work() -> None:
            await asyncio.sleep(0.01)
            done_flag["ran"] = True

        ctx.register(asyncio.create_task(work()))
        await ctx.drain(timeout=1.0)
        assert done_flag["ran"] is True
        assert ctx.pending_tasks() == []

    @pytest.mark.asyncio
    async def test_drain_cancels_tasks_past_timeout(self) -> None:
        ctx = RunContext.from_wall_time(0)

        async def slow() -> None:
            await asyncio.sleep(10.0)

        task = ctx.register(asyncio.create_task(slow()))
        await ctx.drain(timeout=0.02)
        # 超时后剩余任务被取消，不会随进程静默泄漏。
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_completed_task_auto_removed_from_registry(self) -> None:
        ctx = RunContext.from_wall_time(0)

        async def quick() -> None:
            return None

        ctx.register(asyncio.create_task(quick()))
        await asyncio.sleep(0.01)
        # done_callback 应把已完成任务摘除，集合不会无限增长。
        assert ctx.pending_tasks() == []

    @pytest.mark.asyncio
    async def test_cancel_all_sets_event_and_cancels(self) -> None:
        ctx = RunContext.from_wall_time(0)

        async def slow() -> None:
            await asyncio.sleep(10.0)

        task = ctx.register(asyncio.create_task(slow()))
        ctx.cancel_all()
        assert ctx.cancel_event.is_set()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()


class TestBoundedConcurrency:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_execution(self) -> None:
        ctx = RunContext(max_concurrency=2)
        active = {"now": 0, "peak": 0}

        async def work() -> None:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.02)
            active["now"] -= 1

        await asyncio.gather(*(ctx.run_bounded(work()) for _ in range(6)))
        # 并发峰值不得超过配置上限——这就是背压。
        assert active["peak"] <= 2

    def test_default_concurrency_is_bounded(self) -> None:
        ctx = RunContext.from_wall_time(0)
        # 默认也应是有界的小并发，而不是无限。
        assert ctx._semaphore._value == DEFAULT_MAX_CONCURRENCY

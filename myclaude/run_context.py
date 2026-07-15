# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""Per-run deadline, cancellation, and background-task lifecycle.

一次 Agent run 的运行预算此前分散在各处：``asyncio.timeout`` 只包住 LLM stream，
工具执行、MCP、压缩、记忆、子 Agent 和 retry sleep 都可能越过总时限；后台任务
以裸 ``create_task`` 派发、无人 drain。``RunContext`` 把这些收敛成单一事实来源：

- deadline：所有外部等待都从这里读剩余预算，谁也不能睡过总时限。
- cancel_event：统一的取消信号。
- task registry：后台任务登记于此，run 结束时按入口策略 drain 或 cancel。
- semaphore：对并发工具 / MCP / 子 Agent 形成简单背压。

它是一个可独立测试的领域对象——不依赖 Agent，也不依赖具体入口。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import nullcontext
from typing import Any, Awaitable, TypeVar

T = TypeVar("T")

# 并发工具 / 子 Agent 的默认背压上限。本地 Coding Agent 不需要成百上千的并发，
# 少量并发即可覆盖「并行读多个文件」这类场景，又不至于让读工具风暴打满句柄。
DEFAULT_MAX_CONCURRENCY = 8


class RunContext:
    """单次运行的 deadline / 取消 / 后台任务 / 背压上下文。"""

    def __init__(
        self,
        *,
        deadline: float | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        run_id: str | None = None,
    ) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.started_at = time.monotonic()
        # deadline 用 time.monotonic() 时间轴，避免系统时钟回拨影响预算。
        self.deadline = deadline
        self.cancel_event = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    @classmethod
    def from_wall_time(
        cls,
        max_wall_time_seconds: float,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        run_id: str | None = None,
    ) -> "RunContext":
        """按「最大墙钟时间」构造。<=0 表示不设时限（deadline=None）。"""
        deadline = (
            time.monotonic() + max_wall_time_seconds
            if max_wall_time_seconds and max_wall_time_seconds > 0
            else None
        )
        return cls(
            deadline=deadline, max_concurrency=max_concurrency, run_id=run_id
        )

    # ------------------------------------------------------------------
    # deadline
    # ------------------------------------------------------------------

    def remaining(self) -> float | None:
        """剩余预算秒数；无 deadline 时返回 None，已耗尽时返回 0.0。"""
        if self.deadline is None:
            return None
        return max(self.deadline - time.monotonic(), 0.0)

    def expired(self) -> bool:
        rem = self.remaining()
        return rem is not None and rem <= 0

    def timeout_scope(self):
        """返回一个把 await 限制在剩余预算内的上下文管理器。

        无 deadline 时退化为 nullcontext；已耗尽时用一个立即超时的 scope，
        让调用点统一走 TimeoutError 分支，而不是各自判断。
        """
        remaining = self.remaining()
        if remaining is None:
            return nullcontext()
        # remaining 可能为 0.0；asyncio.timeout(0) 会在首个挂起点立即超时。
        return asyncio.timeout(remaining)

    async def sleep(self, seconds: float) -> bool:
        """睡眠至多 ``seconds``，但绝不睡过 deadline。

        返回 True 表示完整睡满（调用方可继续），返回 False 表示被 deadline
        截断（调用方应停止重试而非继续）。这修掉了 retry backoff 可能一觉
        睡过总时限的问题。
        """
        remaining = self.remaining()
        if remaining is None:
            await asyncio.sleep(max(seconds, 0.0))
            return True
        if remaining <= 0:
            return False
        await asyncio.sleep(min(max(seconds, 0.0), remaining))
        return seconds <= remaining

    # ------------------------------------------------------------------
    # 背压
    # ------------------------------------------------------------------

    async def run_bounded(self, coro: Awaitable[T]) -> T:
        """在并发信号量下执行，形成简单背压。"""
        async with self._semaphore:
            return await coro

    # ------------------------------------------------------------------
    # 后台任务登记
    # ------------------------------------------------------------------

    def register(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """登记后台任务，完成后自动摘除（避免集合无限增长与被 GC）。"""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def pending_tasks(self) -> list[asyncio.Task[Any]]:
        return [t for t in self._tasks if not t.done()]

    async def drain(self, timeout: float | None = None) -> None:
        """等待在途后台任务结束；超时后取消剩余任务并回收。

        入口可据此实现「Headless 结束前 drain 后台子 Agent」而不是随
        ``asyncio.run()`` 关闭被静默取消。
        """
        pending = self.pending_tasks()
        if not pending:
            return
        done, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    def cancel_all(self) -> None:
        """置位取消信号并取消所有在途后台任务。"""
        self.cancel_event.set()
        for task in self.pending_tasks():
            task.cancel()

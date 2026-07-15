# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from myclaude.hooks.executors import execute_action
from myclaude.hooks.models import ActionResult, Hook, HookContext, ToolRejectedError

log = logging.getLogger(__name__)


@dataclass
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool


class HookEngine:
    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self.hooks: list[Hook] = hooks or []
        self._prompt_messages: list[str] = []
        self._notifications: list[HookNotification] = []
        # 异步 hook 以 fire-and-forget 方式派发。必须持有强引用，否则 event loop
        # 只保留弱引用，任务可能在运行途中被 GC；同时也让 Runtime 退出前能够
        # drain/cancel，避免外部脚本把未完成的副作用留在进程关闭之后。
        self._async_tasks: set[asyncio.Task] = set()


    def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
        matched: list[Hook] = []
        for hook in self.hooks:
            if hook.event != event:
                continue
            if not hook.should_run():
                continue
            if hook.condition is not None and not hook.condition.evaluate(ctx):
                continue
            matched.append(hook)
        return matched


    async def run_hooks(self, event: str, ctx: HookContext) -> None:
        matched = self.find_matching_hooks(event, ctx)
        for hook in matched:
            hook.mark_executed()
            if hook.async_exec:
                task = asyncio.create_task(self._run_single(hook, ctx))
                self._async_tasks.add(task)
                task.add_done_callback(self._async_tasks.discard)
            else:
                await self._run_single(hook, ctx)


    async def drain_async_hooks(self, timeout: float | None = None) -> None:
        """等待在途异步 hook 结束；超时后取消剩余任务。

        Runtime 退出前调用，确保外部脚本的副作用要么完成、要么被显式取消，
        而不是随进程关闭被静默丢弃。
        """
        pending = [t for t in self._async_tasks if not t.done()]
        if not pending:
            return
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)


    async def _run_single(self, hook: Hook, ctx: HookContext) -> None:
        try:
            result = await execute_action(hook.action, ctx)
            if hook.action.type == "prompt" and result.success:
                self._prompt_messages.append(result.output)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=result.output,
                    success=result.success,
                )
            )
            if not result.success:
                log.warning(
                    "Hook '%s' action failed: %s", hook.id, result.output
                )
        except Exception as e:
            log.warning("Hook '%s' execution error: %s", hook.id, e)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=str(e),
                    success=False,
                )
            )


    async def run_pre_tool_hooks(
        self, ctx: HookContext
    ) -> ToolRejectedError | None:
        matched = self.find_matching_hooks("pre_tool_use", ctx)
        for hook in matched:
            hook.mark_executed()
            try:
                result = await execute_action(hook.action, ctx)
                self._notifications.append(
                    HookNotification(
                        hook_id=hook.id,
                        event="pre_tool_use",
                        output=result.output,
                        success=result.success,
                    )
                )
                if result.decision == "deny" or hook.reject:
                    reason = result.output or (
                        "hook action failed" if not result.success else "rejected"
                    )
                    return ToolRejectedError(
                        tool=ctx.tool_name,
                        reason=reason,
                        hook_id=hook.id,
                    )
            except Exception as e:
                log.warning("Hook '%s' execution error: %s", hook.id, e)
        return None

    def get_prompt_messages(self) -> list[str]:
        messages = list(self._prompt_messages)
        self._prompt_messages.clear()
        return messages


    def drain_notifications(self) -> list[HookNotification]:
        notifications = list(self._notifications)
        self._notifications.clear()
        return notifications

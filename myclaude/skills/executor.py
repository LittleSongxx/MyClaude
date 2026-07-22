from __future__ import annotations

import logging
import inspect
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from myclaude.conversation import ConversationManager, Message
from myclaude.skills.parser import (
    SkillDef,
    expand_dynamic_context,
    substitute_arguments,
)

if TYPE_CHECKING:
    from myclaude.agent import Agent, AgentEvent
    from myclaude.client import LLMClient

log = logging.getLogger(__name__)

FORK_RECENT_COUNT = 5


class SkillExecutor:


    def __init__(
        self,
        agent: Agent,
        client: LLMClient,
        protocol: str,
        permission_handler: Callable[[Any], Awaitable[Any] | Any] | None = None,
    ) -> None:
        self.agent = agent
        self.client = client
        self.protocol = protocol
        self.permission_handler = permission_handler


    def execute_inline(self, skill: SkillDef, args: str) -> None:
        prompt = substitute_arguments(skill.prompt_body, args)
        prompt = expand_dynamic_context(prompt, self.agent.work_dir)
        # activate_skill 内部已统一记录 recovery_state，无需在此重复记录。
        activated = self.agent.activate_skill(
            skill.name,
            prompt,
            allowed_tools=skill.allowed_tools,
            disallowed_tools=skill.disallowed_tools,
        )
        conversation = getattr(self.agent, "_current_conversation", None)
        if activated and conversation is not None:
            conversation.add_system_reminder(f"# Skill: {skill.name}\n\n{prompt}")


    async def execute_fork(
        self, skill: SkillDef, args: str
    ) -> str:
        prompt = substitute_arguments(skill.prompt_body, args)
        prompt = expand_dynamic_context(prompt, self.agent.work_dir)
        if getattr(self.agent, "recovery_state", None) is not None:
            self.agent.recovery_state.record_skill_invocation(
                skill.name, skill.prompt_body
            )

        fork_conv = ConversationManager()

        context_messages = self._build_fork_context(skill.context)
        for msg in context_messages:
            if msg.role == "user":
                fork_conv.add_user_message(msg.content)
            else:
                fork_conv.add_assistant_message(msg.content)

        fork_conv.add_user_message(prompt)

        from myclaude.agent import (
            Agent as AgentClass,
            ErrorEvent,
            LoopComplete,
            PermissionRequest,
            PermissionResponse,
            StreamText,
        )

        from myclaude.agents.tool_filter import clone_registry_for_fork

        fork_registry = clone_registry_for_fork(self.agent.registry)
        fork_agent = AgentClass(
            client=self.client,
            registry=fork_registry,
            protocol=self.protocol,
            work_dir=self.agent.work_dir,
            max_iterations=self.agent.max_iterations,
            permission_checker=self.agent.permission_checker,
            context_window=self.agent.context_window,
            instructions_content=self.agent.instructions_content,
            memory_manager=self.agent.memory_manager,
            hook_engine=self.agent.hook_engine,
            run_limits=getattr(self.agent, "run_limits", None),
            instruction_resolver=getattr(self.agent, "instruction_resolver", None),
            enable_runtime_contracts=True,
            persist_runtime_contracts=False,
        )
        if getattr(fork_agent, "context_ledger", None) is not None:
            from myclaude.tools.context_ledger import UpdateContextLedgerTool

            fork_registry.register(
                UpdateContextLedgerTool(
                    fork_agent.context_ledger,
                    fork_agent.verification_gate,
                )
            )
        fork_agent.activate_skill(
            skill.name,
            prompt,
            allowed_tools=skill.allowed_tools,
            disallowed_tools=skill.disallowed_tools,
        )

        result_parts: list[str] = []
        async for event in fork_agent.run(fork_conv):
            if isinstance(event, StreamText):
                result_parts.append(event.text)
            elif isinstance(event, ErrorEvent):
                result_parts.append(f"\n[Error: {event.message}]")
            elif isinstance(event, PermissionRequest):
                if self.permission_handler is None:
                    response = PermissionResponse.DENY
                else:
                    response = self.permission_handler(event)
                    if inspect.isawaitable(response):
                        response = await response
                    response = response or PermissionResponse.DENY
                if not event.future.done():
                    event.future.set_result(response)
            elif isinstance(event, LoopComplete):
                break

        return "".join(result_parts)


    def _build_fork_context(self, mode: str) -> list[Message]:
        if mode == "none":
            return []

        conversation = getattr(self.agent, "_current_conversation", None)
        history = conversation.history if conversation is not None else []
        if not history:
            main_history = []
        else:
            main_history = history

        if mode == "recent":
            content_messages = [
                m for m in main_history
                if m.content and not m.tool_results
            ]
            return content_messages[-FORK_RECENT_COUNT:]

        if mode == "full":
            content_messages = [
                m for m in main_history
                if m.content and not m.tool_results
            ]
            if not content_messages:
                return []
            summary_parts = []
            for m in content_messages:
                prefix = "User" if m.role == "user" else "Assistant"
                text = m.content[:200]
                if len(m.content) > 200:
                    text += "..."
                summary_parts.append(f"{prefix}: {text}")
            summary = "## Previous conversation summary\n\n" + "\n\n".join(summary_parts)
            return [Message(role="user", content=summary)]

        return []

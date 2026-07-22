from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from myclaude.agent import Agent, PermissionRequest, PermissionResponse
    from myclaude.agents.trace import TraceManager
    from myclaude.conversation import ConversationManager

log = logging.getLogger(__name__)

PermissionHandler = Callable[
    ["PermissionRequest"],
    Awaitable["PermissionResponse | None"] | "PermissionResponse | None",
]


@dataclass
class ProgressInfo:
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_activity: str = ""


@dataclass
class BackgroundTask:
    id: str
    name: str
    agent: Agent | None
    task: str
    status: str = "running"
    result: str = ""
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    cancel: Callable[[], None] | None = None
    progress: ProgressInfo = field(default_factory=ProgressInfo)
    transcript_path: str = ""
    agent_id: str = ""
    agent_type: str = ""
    conversation: ConversationManager | None = field(default=None, repr=False)


class TaskManager:
    """Run background agents and preserve enough state to inspect or resume them."""

    def __init__(self, work_dir: str | Path | None = None) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()
        self._async_tasks: dict[str, asyncio.Task[None]] = {}
        self._permission_handler: PermissionHandler | None = None
        self._permission_lock = asyncio.Lock()
        self._trace_manager: TraceManager | None = None
        self._storage_dir: Path | None = None
        self._manifest_path: Path | None = None
        if work_dir is not None:
            self.configure_storage(work_dir)

    def configure_storage(self, work_dir: str | Path) -> None:
        storage = Path(work_dir).expanduser().resolve() / ".myclaude" / "agents"
        storage.mkdir(parents=True, exist_ok=True)
        storage.chmod(0o700)
        transcript_dir = storage / "transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_dir.chmod(0o700)
        self._storage_dir = storage
        self._manifest_path = storage / "tasks.json"
        self._load_manifest()

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        self._permission_handler = handler

    def set_trace_manager(self, manager: TraceManager) -> None:
        self._trace_manager = manager

    async def handle_permission_request(
        self, request: PermissionRequest
    ) -> PermissionResponse | None:
        """Serialize child prompts and hand them to the owning user interface."""
        from myclaude.agent import PermissionResponse

        async with self._permission_lock:
            if self._permission_handler is None:
                return PermissionResponse.DENY
            response = self._permission_handler(request)
            if inspect.isawaitable(response):
                response = await response
            if response is not None and not request.future.done():
                request.future.set_result(response)
            if not request.future.done():
                try:
                    return await request.future
                except asyncio.CancelledError:
                    if not request.future.done():
                        request.future.set_result(PermissionResponse.DENY)
                    raise
            return request.future.result()

    def launch(
        self,
        agent: Agent,
        task: str,
        name: str = "",
        fork_conversation: Any = None,
    ) -> str:
        from myclaude.conversation import ConversationManager

        task_id = uuid.uuid4().hex[:8]
        transcript = ""
        if self._storage_dir is not None:
            transcript = str(self._storage_dir / "transcripts" / f"agent-{task_id}.jsonl")
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task,
            transcript_path=transcript,
            agent_id=getattr(agent, "agent_id", ""),
            agent_type=getattr(agent, "_agent_type", ""),
            conversation=fork_conversation or ConversationManager(),
        )
        self._tasks[task_id] = bg
        self._append_transcript(bg, {"type": "task_started", "task": task})
        self._persist_manifest()
        self._start_task(bg)
        return task_id

    def _start_task(self, bg: BackgroundTask) -> None:
        async_task = asyncio.create_task(self._run_background(bg.id))
        self._async_tasks[bg.id] = async_task
        bg.cancel = async_task.cancel

    async def _run_background(self, task_id: str) -> None:
        bg = self._tasks.get(task_id)
        if bg is None or bg.agent is None:
            return

        pending_text: list[str] = []

        def flush_text() -> None:
            if pending_text:
                self._append_transcript(
                    bg, {"type": "stream_text", "text": "".join(pending_text)}
                )
                pending_text.clear()

        def on_event(event: dict[str, Any]) -> None:
            event_type = str(event.get("type", "event"))
            bg.progress.last_activity = event_type
            if event_type == "stream_text":
                pending_text.append(str(event.get("text", "")))
                return
            flush_text()
            if event_type == "tool_use":
                bg.progress.tool_call_count += 1
            if event_type in {"turn_complete", "loop_complete"}:
                event = {
                    **event,
                    "conversation": self._serialize_conversation(bg.conversation),
                }
            self._append_transcript(bg, event)

        try:
            result = await bg.agent.run_to_completion(
                bg.task,
                bg.conversation,
                event_callback=on_event,
                permission_handler=self.handle_permission_request,
            )
            bg.result = (bg.result + "\n" + result).strip() if bg.result else result
            bg.status = "completed"
            await self._run_teammate_idle_loop(bg, on_event)
        except asyncio.CancelledError:
            bg.status = "cancelled"
            bg.result = "Task was cancelled"
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            bg.status = "failed"
            bg.result = f"Error: {e}"
        finally:
            flush_text()
            bg.end_time = time.time()
            if bg.agent is not None:
                bg.progress.input_tokens = bg.agent.total_input_tokens
                bg.progress.output_tokens = bg.agent.total_output_tokens
                if self._trace_manager is not None and bg.agent_id:
                    self._trace_manager.update(
                        bg.agent_id,
                        input_tokens=bg.progress.input_tokens,
                        output_tokens=bg.progress.output_tokens,
                        tool_call_count=bg.progress.tool_call_count,
                    )
                    self._trace_manager.complete(bg.agent_id, bg.status)
            self._append_transcript(
                bg,
                {
                    "type": "task_finished",
                    "status": bg.status,
                    "result": bg.result,
                    "conversation": self._serialize_conversation(bg.conversation),
                },
            )
            self._persist_manifest()
            self._async_tasks.pop(task_id, None)
            await self._notify_queue.put(task_id)

    async def _run_teammate_idle_loop(
        self, bg: BackgroundTask, on_event: Callable[[dict[str, Any]], None]
    ) -> None:
        if bg.agent is None or not bg.agent.team_name or not bg.agent._team_manager:
            return
        mailbox = bg.agent._team_manager.get_mailbox(bg.agent.team_name)
        if not mailbox:
            return
        from myclaude.teams.mailbox import create_message

        mailbox.write(
            "lead",
            create_message(
                from_agent=bg.name,
                to_agent="lead",
                content=f"[idle] {bg.name}: completed initial task",
                summary=f"{bg.name} idle",
            ),
        )
        for _ in range(60):
            await asyncio.sleep(1)
            messages = mailbox.consume(bg.agent.agent_id)
            if not messages:
                continue
            prompt = "\n\n".join(
                f"[Message from {message.from_agent}] {message.content}"
                for message in messages
            )
            bg.result = await bg.agent.run_to_completion(
                prompt,
                bg.conversation,
                event_callback=on_event,
                permission_handler=self.handle_permission_request,
            )
            mailbox.write(
                "lead",
                create_message(
                    from_agent=bg.name,
                    to_agent="lead",
                    content=f"[idle] {bg.name}: completed follow-up",
                    summary=f"{bg.name} idle",
                ),
            )

    def resume(self, task_id: str, prompt: str, *, agent: Agent | None = None) -> str:
        bg = self._tasks.get(task_id)
        if bg is None:
            raise KeyError(f"unknown task ID '{task_id}'")
        if bg.status == "running":
            raise RuntimeError(f"task '{task_id}' is already running")
        if not prompt.strip():
            raise ValueError("a non-empty follow-up prompt is required")
        selected_agent = agent or bg.agent
        if selected_agent is None:
            raise RuntimeError("the original agent is unavailable; provide a replacement agent")
        bg.agent = selected_agent
        bg.agent_id = getattr(selected_agent, "agent_id", bg.agent_id)
        bg.agent_type = getattr(selected_agent, "_agent_type", bg.agent_type)
        bg.task = prompt
        bg.status = "running"
        bg.result = ""
        bg.start_time = time.time()
        bg.end_time = None
        if bg.conversation is None:
            bg.conversation = self._load_conversation(bg.transcript_path)
        self._append_transcript(bg, {"type": "task_resumed", "task": prompt})
        self._persist_manifest()
        self._start_task(bg)
        return task_id

    def adopt_running(
        self,
        agent: Agent,
        task_description: str,
        partial_result: str = "",
        name: str = "",
    ) -> str:
        task_id = self.launch(agent, task_description, name=name)
        bg = self._tasks[task_id]
        bg.result = partial_result
        return task_id

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def cancel(self, task_id: str) -> bool:
        bg = self._tasks.get(task_id)
        if bg is None or bg.status != "running":
            return False
        async_task = self._async_tasks.get(task_id)
        if async_task and not async_task.done():
            async_task.cancel()
            return True
        return False

    def poll_completed(self) -> list[BackgroundTask]:
        completed: list[BackgroundTask] = []
        while not self._notify_queue.empty():
            try:
                task_id = self._notify_queue.get_nowait()
                bg = self._tasks.get(task_id)
                if bg is not None:
                    completed.append(bg)
            except asyncio.QueueEmpty:
                break
        return completed

    def pending_async_tasks(self) -> list[asyncio.Task[None]]:
        return [task for task in self._async_tasks.values() if not task.done()]

    async def drain(self, timeout: float | None = None) -> None:
        pending = self.pending_async_tasks()
        if not pending:
            return
        _, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    def _append_transcript(self, bg: BackgroundTask, event: dict[str, Any]) -> None:
        if not bg.transcript_path:
            return
        record = {"timestamp": time.time(), **event}
        try:
            path = Path(bg.transcript_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as e:
            log.warning("Could not append sub-agent transcript %s: %s", bg.id, e)

    @staticmethod
    def _serialize_conversation(conversation: ConversationManager | None) -> list[dict[str, Any]]:
        if conversation is None:
            return []
        return [asdict(message) for message in conversation.history]

    @staticmethod
    def _load_conversation(transcript_path: str) -> ConversationManager:
        from myclaude.conversation import (
            ConversationManager,
            Message,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
        )

        conversation = ConversationManager()
        if not transcript_path:
            return conversation
        try:
            lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return conversation
        records: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row.get("conversation"), list):
                records = row["conversation"]
                break
        for row in records:
            conversation.history.append(
                Message(
                    role=str(row.get("role", "user")),
                    content=str(row.get("content", "")),
                    tool_uses=[ToolUseBlock(**item) for item in row.get("tool_uses", [])],
                    tool_results=[
                        ToolResultBlock(**item) for item in row.get("tool_results", [])
                    ],
                    thinking_blocks=[
                        ThinkingBlock(**item) for item in row.get("thinking_blocks", [])
                    ],
                    source=str(row.get("source", "")),
                )
            )
        conversation.env_injected = any(
            message.source == "environment" for message in conversation.history
        )
        conversation.ltm_injected = any(
            message.source == "system_reminder"
            and ("# myclaudeMd" in message.content or "# autoMemory" in message.content)
            for message in conversation.history
        )
        return conversation

    def _manifest_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bg in self._tasks.values():
            rows.append(
                {
                    "id": bg.id,
                    "name": bg.name,
                    "task": bg.task,
                    "status": bg.status,
                    "result": bg.result,
                    "start_time": bg.start_time,
                    "end_time": bg.end_time,
                    "progress": asdict(bg.progress),
                    "transcript_path": bg.transcript_path,
                    "agent_id": bg.agent_id,
                    "agent_type": bg.agent_type,
                }
            )
        return rows

    def _persist_manifest(self) -> None:
        if self._manifest_path is None:
            return
        temp = self._manifest_path.with_suffix(".tmp")
        try:
            temp.write_text(
                json.dumps({"version": 1, "tasks": self._manifest_rows()}, indent=2),
                encoding="utf-8",
            )
            temp.chmod(0o600)
            os.replace(temp, self._manifest_path)
        except OSError as e:
            log.warning("Could not persist sub-agent task state: %s", e)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_manifest(self) -> None:
        if self._manifest_path is None or not self._manifest_path.exists():
            return
        try:
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not load sub-agent task state: %s", e)
            return
        changed = False
        for row in payload.get("tasks", []):
            task_id = str(row.get("id", ""))
            if not task_id or task_id in self._tasks:
                continue
            status = str(row.get("status", "failed"))
            if status == "running":
                status = "detached"
                changed = True
            progress_data = row.get("progress", {})
            bg = BackgroundTask(
                id=task_id,
                name=str(row.get("name", task_id)),
                agent=None,
                task=str(row.get("task", "")),
                status=status,
                result=str(row.get("result", "")),
                start_time=float(row.get("start_time", time.time())),
                end_time=row.get("end_time"),
                progress=ProgressInfo(
                    tool_call_count=int(progress_data.get("tool_call_count", 0)),
                    input_tokens=int(progress_data.get("input_tokens", 0)),
                    output_tokens=int(progress_data.get("output_tokens", 0)),
                    last_activity=str(progress_data.get("last_activity", "")),
                ),
                transcript_path=str(row.get("transcript_path", "")),
                agent_id=str(row.get("agent_id", "")),
                agent_type=str(row.get("agent_type", "")),
            )
            bg.conversation = self._load_conversation(bg.transcript_path)
            self._tasks[task_id] = bg
        if changed:
            self._persist_manifest()

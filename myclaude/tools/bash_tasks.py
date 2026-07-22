from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, Tool, ToolResult
from myclaude.tools.process_manager import ProcessManager


class OutputParams(BaseModel):
    task_id: str = Field(default="", description="Task ID; omit to list tasks")
    offset: int = Field(default=0, ge=0, description="Character offset in output")
    limit: int = Field(default=30_000, ge=1, le=150_000)


class BashOutput(Tool):
    name = "BashOutput"
    description = "List background shell tasks or read paginated output for one task."
    params_model = OutputParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, manager: ProcessManager) -> None:
        self.manager = manager

    async def execute(self, params: OutputParams) -> ToolResult:
        self.manager.configure(self.work_dir or ".")
        if not params.task_id:
            tasks = self.manager.list()
            if not tasks:
                return ToolResult(output="No background shell tasks.")
            return ToolResult(output="\n".join(
                f"{task.task_id}\t{task.status}\tpid={task.pid}\t{task.command}"
                for task in tasks
            ))
        task = self.manager.get(params.task_id)
        if task is None:
            return ToolResult(output=f"Error: unknown background task {params.task_id}", is_error=True)
        path = Path(task.output_path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return ToolResult(output=f"Error reading task output: {error}", is_error=True)
        page = text[params.offset : params.offset + params.limit]
        next_offset = params.offset + len(page)
        header = (
            f"Task {task.task_id}: status={task.status}, pid={task.pid}, "
            f"exit={task.returncode}\nOutput file: {path}\n\n"
        )
        if next_offset < len(text):
            page += f"\n\n[PARTIAL output. Continue with offset={next_offset}.]"
        return ToolResult(
            output=header + (page or "(no output yet)"),
            artifact_path=str(path),
            truncated=next_offset < len(text),
            total_bytes=path.stat().st_size,
            next_offset=next_offset if next_offset < len(text) else None,
            metadata={"task_id": task.task_id, "status": task.status},
        )


class StopParams(BaseModel):
    task_id: str


class BashStop(Tool):
    name = "BashStop"
    description = "Stop a running background shell task and its child processes."
    params_model = StopParams
    category = "command"

    def __init__(self, manager: ProcessManager) -> None:
        self.manager = manager

    def permission_scope(self, arguments: dict[str, object]) -> PermissionScope:
        task_id = str(arguments.get("task_id", ""))
        return PermissionScope(content=f"stop background task {task_id}")

    async def execute(self, params: StopParams) -> ToolResult:
        self.manager.configure(self.work_dir or ".")
        stopped = await self.manager.stop(params.task_id)
        if not stopped:
            return ToolResult(
                output=f"Error: task {params.task_id} is not running or does not exist",
                is_error=True,
            )
        return ToolResult(output=f"Stopped background task {params.task_id}")

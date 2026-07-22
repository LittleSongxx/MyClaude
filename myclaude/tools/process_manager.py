from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from myclaude.tools.file_io import atomic_write_text


@dataclass
class ProcessTask:
    task_id: str
    command: str
    cwd: str
    output_path: str
    pid: int
    status: str = "running"
    returncode: int | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    output_handle: BinaryIO | None = field(default=None, repr=False)
    waiter: asyncio.Task[None] | None = field(default=None, repr=False)
    cwd_marker: str = ""

    def persisted(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "command": self.command,
            "cwd": self.cwd,
            "output_path": self.output_path,
            "pid": self.pid,
            "status": self.status,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cwd_marker": self.cwd_marker,
        }


async def terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
            return
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
    else:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except (FileNotFoundError, OSError):
            proc.kill()
    await proc.wait()


class ProcessManager:
    """Own background shell processes and their durable output metadata."""

    def __init__(self) -> None:
        self._root: Path | None = None
        self._tasks: dict[str, ProcessTask] = {}

    def configure(self, work_dir: str) -> None:
        root = Path(work_dir).expanduser().resolve() / ".myclaude" / "processes"
        if self._root == root:
            return
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        self._root = root
        self._load_metadata()

    @property
    def root(self) -> Path:
        if self._root is None:
            raise RuntimeError("process manager has no working directory")
        return self._root

    @property
    def metadata_path(self) -> Path:
        return self.root / "tasks.json"

    def _load_metadata(self) -> None:
        self._tasks = {}
        if not self.metadata_path.is_file():
            return
        try:
            rows = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                task = ProcessTask(**row)
            except TypeError:
                continue
            if task.status == "running":
                task.status = "detached"
            self._tasks[task.task_id] = task

    def _persist(self) -> None:
        rows = [task.persisted() for task in self._tasks.values()]
        atomic_write_text(self.metadata_path, json.dumps(rows, indent=2, ensure_ascii=False))

    def create_output_file(self) -> tuple[Path, BinaryIO]:
        task_stem = uuid.uuid4().hex
        path = self.root / f"{task_stem}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return path, os.fdopen(fd, "wb", buffering=0)

    def adopt(
        self,
        proc: asyncio.subprocess.Process,
        *,
        command: str,
        cwd: str,
        output_path: Path,
        output_handle: BinaryIO,
        cwd_marker: str = "",
    ) -> ProcessTask:
        task_id = uuid.uuid4().hex[:10]
        task = ProcessTask(
            task_id=task_id,
            command=command,
            cwd=cwd,
            output_path=str(output_path),
            pid=proc.pid,
            process=proc,
            output_handle=output_handle,
            cwd_marker=cwd_marker,
        )
        self._tasks[task_id] = task
        task.waiter = asyncio.create_task(self._watch(task))
        self._persist()
        return task

    async def _watch(self, task: ProcessTask) -> None:
        try:
            assert task.process is not None
            task.returncode = await task.process.wait()
            task.status = "completed" if task.returncode == 0 else "failed"
            task.ended_at = time.time()
            self._strip_marker(task)
        except asyncio.CancelledError:
            # The child owns a duplicate output descriptor and may outlive the UI.
            raise
        finally:
            if task.output_handle is not None:
                task.output_handle.close()
                task.output_handle = None
            self._persist()

    @staticmethod
    def _strip_marker(task: ProcessTask) -> None:
        if not task.cwd_marker:
            return
        path = Path(task.output_path)
        marker = ("\n" + task.cwd_marker).encode("utf-8")
        try:
            size = path.stat().st_size
            with path.open("r+b") as handle:
                start = max(0, size - 16_384)
                handle.seek(start)
                tail = handle.read()
                index = tail.rfind(marker)
                if index >= 0:
                    handle.truncate(start + index)
        except OSError:
            return

    def get(self, task_id: str) -> ProcessTask | None:
        return self._tasks.get(task_id)

    def list(self) -> list[ProcessTask]:
        return sorted(self._tasks.values(), key=lambda task: task.started_at, reverse=True)

    async def stop(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.process is None or task.status != "running":
            return False
        await terminate_process_tree(task.process)
        if task.waiter is not None:
            await asyncio.gather(task.waiter, return_exceptions=True)
        task.status = "stopped"
        task.returncode = task.process.returncode
        task.ended_at = time.time()
        self._persist()
        return True


def process_creation_kwargs() -> dict[str, object]:
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

from __future__ import annotations

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, Tool, ToolResult
from myclaude.tools.process_manager import (
    ProcessManager,
    process_creation_kwargs,
    terminate_process_tree,
)

if TYPE_CHECKING:
    from myclaude.sandbox import Sandbox, SandboxConfig

MAX_TIMEOUT = 600
INLINE_OUTPUT_BYTES = 30_000

_COMMAND_ERROR_THRESHOLDS: dict[str, int] = {
    "grep": 2, "egrep": 2, "fgrep": 2, "rg": 2,
    "diff": 2, "find": 2, "test": 2, "[": 2,
}
_EXIT_CODE_HINTS: dict[str, str] = {
    "grep": "no matches found", "egrep": "no matches found",
    "fgrep": "no matches found", "rg": "no matches found",
    "diff": "files differ", "find": "some directories were inaccessible",
    "test": "condition is false", "[": "condition is false",
}


def _extract_last_command_name(command: str) -> str | None:
    last_segment = command.rsplit("|", maxsplit=1)[-1].strip()
    if not last_segment:
        return None
    try:
        tokens = shlex.split(last_segment)
    except ValueError:
        tokens = last_segment.split()
    for token in tokens:
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        return token.rsplit("/", maxsplit=1)[-1]
    return None


def _interpret_exit_code(command: str, exit_code: int) -> bool:
    if exit_code == 0:
        return False
    command_name = _extract_last_command_name(command)
    threshold = _COMMAND_ERROR_THRESHOLDS.get(command_name or "")
    return exit_code >= threshold if threshold is not None else True


def _exit_code_hint(command: str, exit_code: int) -> str:
    command_name = _extract_last_command_name(command)
    hint = _EXIT_CODE_HINTS.get(command_name or "", "")
    return f"Exit code {exit_code}" + (f" ({hint})" if hint else "")


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, ge=1, le=MAX_TIMEOUT, description="Foreground timeout in seconds")
    run_in_background: bool = Field(default=False, description="Start as a background task")


class Bash(Tool):
    name = "Bash"
    description = (
        "Execute a shell command. Long commands can run in the background; timed-out "
        "commands continue as tasks whose output is available through BashOutput."
    )
    params_model = Params
    category = "command"
    interrupt_behavior = "cancel"

    work_dir: str | None = None
    sandbox: Sandbox | None = None
    sandbox_config: SandboxConfig | None = None

    def __init__(self, process_manager: ProcessManager | None = None) -> None:
        self.process_manager = process_manager or ProcessManager()
        self._project_root: Path | None = None

    def permission_scope(self, arguments: dict[str, object]) -> PermissionScope:
        return PermissionScope(content=str(arguments.get("command", "")))

    def _configure(self) -> None:
        work_dir = str(Path(self.work_dir or ".").expanduser().resolve())
        if self._project_root is None:
            self._project_root = Path(work_dir)
        self.process_manager.configure(str(self._project_root))

    @staticmethod
    def _instrument(command: str, marker: str) -> str:
        return (
            "{\n" + command + "\n}\n"
            "__myclaude_status=$?\n"
            f"printf '\\n{marker}%s\\n' \"$PWD\"\n"
            "exit $__myclaude_status"
        )

    def _extract_cwd(self, output_path: Path, marker: str) -> str:
        marker_bytes = ("\n" + marker).encode("utf-8")
        try:
            size = output_path.stat().st_size
            with output_path.open("r+b") as handle:
                start = max(0, size - 16_384)
                handle.seek(start)
                tail = handle.read()
                index = tail.rfind(marker_bytes)
                if index < 0:
                    return ""
                absolute = start + index
                value_start = index + len(marker_bytes)
                value_end = tail.find(b"\n", value_start)
                if value_end < 0:
                    return ""
                cwd = tail[value_start:value_end].decode("utf-8", errors="replace")
                handle.truncate(absolute)
                return cwd
        except OSError:
            return ""

    def _apply_cwd(self, candidate: str) -> str:
        if not candidate or self._project_root is None:
            return ""
        try:
            resolved = Path(candidate).resolve()
            resolved.relative_to(self._project_root)
        except (OSError, ValueError):
            self.work_dir = str(self._project_root)
            return f"\n\n[Shell cwd was reset to {self._project_root}]"
        self.work_dir = str(resolved)
        return ""

    @staticmethod
    def _read_preview(path: Path) -> tuple[str, int, bool]:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= INLINE_OUTPUT_BYTES:
                data = handle.read()
                truncated = False
            else:
                half = INLINE_OUTPUT_BYTES // 2
                head = handle.read(half)
                handle.seek(max(0, size - half))
                tail = handle.read(half)
                data = head + b"\n\n[... middle output omitted ...]\n\n" + tail
                truncated = True
        return data.decode(errors="replace"), size, truncated

    def _background_result(self, task_id: str, output_path: Path, reason: str) -> ToolResult:
        return ToolResult(
            output=(
                f"{reason}\nTask ID: {task_id}\nOutput file: {output_path}\n"
                "Use BashOutput to inspect progress and BashStop to terminate it."
            ),
            artifact_path=str(output_path),
            metadata={"task_id": task_id, "status": "running"},
        )

    async def execute(self, params: Params) -> ToolResult:
        self._configure()
        marker = f"__MYCLAUDE_CWD_{os.urandom(12).hex()}__="
        instrumented = self._instrument(params.command, marker)
        actual_command = instrumented
        if self.sandbox and self.sandbox_config and self.sandbox.available():
            actual_command = self.sandbox.wrap(instrumented, self.sandbox_config)

        output_path, output_handle = self.process_manager.create_output_file()
        proc: asyncio.subprocess.Process | None = None
        adopted = False
        try:
            env = {
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "",
                "GIT_PAGER": "cat", "PAGER": "cat", "NO_COLOR": "1",
            }
            proc = await asyncio.create_subprocess_shell(
                actual_command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output_handle,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.work_dir,
                env=env,
                **process_creation_kwargs(),
            )
            if params.run_in_background:
                task = self.process_manager.adopt(
                    proc,
                    command=params.command,
                    cwd=self.work_dir or ".",
                    output_path=output_path,
                    output_handle=output_handle,
                    cwd_marker=marker,
                )
                adopted = True
                return self._background_result(
                    task.task_id, output_path, "Command started in the background."
                )
            try:
                await asyncio.wait_for(proc.wait(), timeout=params.timeout)
            except asyncio.TimeoutError:
                task = self.process_manager.adopt(
                    proc,
                    command=params.command,
                    cwd=self.work_dir or ".",
                    output_path=output_path,
                    output_handle=output_handle,
                    cwd_marker=marker,
                )
                adopted = True
                return self._background_result(
                    task.task_id,
                    output_path,
                    f"Command did not finish within {params.timeout}s and was moved to the background.",
                )
        except asyncio.CancelledError:
            if proc is not None:
                await terminate_process_tree(proc)
            raise
        except Exception as error:
            if proc is not None:
                await terminate_process_tree(proc)
            return ToolResult(output=f"Error executing command: {error}", is_error=True)
        finally:
            if not adopted and not output_handle.closed:
                output_handle.close()

        assert proc is not None
        cwd = self._extract_cwd(output_path, marker)
        cwd_note = self._apply_cwd(cwd)
        output, total_bytes, truncated = self._read_preview(output_path)
        exit_code = proc.returncode or 0
        is_error = _interpret_exit_code(params.command, exit_code)
        if exit_code != 0:
            output = (output.rstrip() + "\n\n" if output else "") + _exit_code_hint(params.command, exit_code)
        if not output:
            output = "(no output)"
        output += cwd_note

        artifact_path = str(output_path) if truncated else ""
        if not truncated:
            try:
                output_path.unlink()
            except OSError:
                pass
        return ToolResult(
            output=output,
            is_error=is_error,
            artifact_path=artifact_path,
            truncated=truncated,
            total_bytes=total_bytes,
            metadata={"exit_code": exit_code, "cwd": self.work_dir or ""},
        )

from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
import subprocess
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, Tool, ToolResult

if TYPE_CHECKING:
    from myclaude.sandbox import Sandbox, SandboxConfig

MAX_TIMEOUT = 600
MAX_CAPTURE_BYTES = 1_000_000
READ_CHUNK_BYTES = 64 * 1024

# 特殊命令的退出码语义映射
# 这些命令的 exit code 1 不代表错误，只有 >= 阈值才算真正的错误
# 例如 grep 返回 1 仅表示"没有匹配行"，不是执行出错
_COMMAND_ERROR_THRESHOLDS: dict[str, int] = {
    "grep": 2,   # exit 1 = 没有匹配到内容
    "egrep": 2,
    "fgrep": 2,
    "rg": 2,     # ripgrep，与 grep 语义一致
    "diff": 2,   # exit 1 = 文件内容有差异
    "find": 2,   # exit 1 = 部分成功（如权限不足跳过某些目录）
    "test": 2,   # exit 1 = 条件为假
    "[": 2,      # test 的别名形式
}


def _extract_last_command_name(command: str) -> str | None:
    """从命令字符串中提取最后一个管道段的基础命令名。

    管道中最后一个命令决定了整体退出码，所以只看最后一段。
    例如 "cat file | grep pattern" → "grep"
    """
    # 按管道符拆分，取最后一段
    last_segment = command.rsplit("|", maxsplit=1)[-1].strip()
    if not last_segment:
        return None

    # 跳过常见的环境变量赋值前缀，如 "FOO=bar command ..."
    # 也要处理 sudo/env 等包装命令
    try:
        tokens = shlex.split(last_segment)
    except ValueError:
        # shlex 解析失败时，用简单的空格分割兜底
        tokens = last_segment.split()

    for token in tokens:
        # 跳过形如 VAR=VALUE 的环境变量赋值
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        # 取 basename（去掉路径前缀，如 /usr/bin/grep → grep）
        base = token.rsplit("/", maxsplit=1)[-1]
        return base

    return None


def _interpret_exit_code(command: str, exit_code: int) -> bool:
    """根据命令语义判断退出码是否代表真正的错误。

    返回 True 表示是错误，False 表示不是错误。
    """
    if exit_code == 0:
        return False

    cmd_name = _extract_last_command_name(command)
    if cmd_name and cmd_name in _COMMAND_ERROR_THRESHOLDS:
        # 只有退出码 >= 阈值时才视为错误
        return exit_code >= _COMMAND_ERROR_THRESHOLDS[cmd_name]

    # 默认行为：非零退出码即为错误
    return True


# 特殊命令的退出码提示信息
# 帮助 LLM 理解非零退出码的含义，而不是简单地标记为错误
_EXIT_CODE_HINTS: dict[str, str] = {
    "grep": "no matches found",
    "egrep": "no matches found",
    "fgrep": "no matches found",
    "rg": "no matches found",
    "diff": "files differ",
    "find": "some directories were inaccessible",
    "test": "condition is false",
    "[": "condition is false",
}


def _exit_code_hint(command: str, exit_code: int) -> str:
    """为非零退出码生成可读提示。

    对于特殊命令（grep/diff/test 等），附加语义说明让 LLM 理解退出码含义。
    普通命令只显示退出码数字。
    """
    cmd_name = _extract_last_command_name(command)
    hint = _EXIT_CODE_HINTS.get(cmd_name, "") if cmd_name else ""
    if hint:
        return f"Exit code {exit_code} ({hint})"
    return f"Exit code {exit_code}"


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, ge=1, le=MAX_TIMEOUT, description="Timeout in seconds (max 600)")


async def _read_bounded(
    stream: asyncio.StreamReader,
) -> tuple[bytes, int]:
    """Drain a process stream without retaining unbounded output in memory."""
    captured = bytearray()
    discarded = 0
    while True:
        chunk = await stream.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        remaining = MAX_CAPTURE_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        discarded += max(0, len(chunk) - max(remaining, 0))
    return bytes(captured), discarded


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate the spawned shell and its descendants, then reap it."""
    if proc.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.5)
            return
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
    else:
        # CREATE_NEW_PROCESS_GROUP gives the process its own console group.  A
        # forced taskkill is the only reliable way to include descendants on
        # Windows; fall back to proc.kill when taskkill is unavailable.
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except (FileNotFoundError, OSError):
            proc.kill()
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


class Bash(Tool):
    name = "Bash"
    description = "Execute a shell command and return stdout and stderr."
    params_model = Params
    category = "command"
    interrupt_behavior = "cancel"

    def permission_scope(self, arguments: dict[str, object]) -> PermissionScope:
        return PermissionScope(content=str(arguments.get("command", "")))

    # 工作目录，为 None 时使用当前进程的工作目录
    work_dir: str | None = None

    # OS 级沙箱实例和配置（由外部注入，为 None 时不启用沙箱）
    sandbox: Sandbox | None = None
    sandbox_config: SandboxConfig | None = None

    async def execute(self, params: Params) -> ToolResult:
        timeout = params.timeout

        # 如果启用了 OS 沙箱，将命令包装为沙箱内执行
        actual_command = params.command
        if self.sandbox and self.sandbox_config and self.sandbox.available():
            actual_command = self.sandbox.wrap(params.command, self.sandbox_config)

        proc: asyncio.subprocess.Process | None = None
        reader_task: asyncio.Task[tuple[bytes, int]] | None = None
        try:
            process_kwargs: dict[str, object] = {}
            if os.name == "posix":
                process_kwargs["start_new_session"] = True
            else:
                process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            env = {
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "NO_COLOR": "1",
            }
            proc = await asyncio.create_subprocess_shell(
                actual_command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # 合并 stderr 到 stdout
                cwd=self.work_dir,
                env=env,
                **process_kwargs,
            )
            assert proc.stdout is not None
            reader_task = asyncio.create_task(_read_bounded(proc.stdout))

            async def wait_and_drain() -> tuple[bytes, int]:
                await proc.wait()
                return await reader_task

            stdout, discarded = await asyncio.wait_for(
                wait_and_drain(), timeout=timeout
            )
        except asyncio.TimeoutError:
            if proc is not None:
                await _terminate_process_tree(proc)
            if reader_task is not None:
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except asyncio.CancelledError:
            if proc is not None:
                await _terminate_process_tree(proc)
            if reader_task is not None:
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)
            raise
        except Exception as e:
            if proc is not None:
                await _terminate_process_tree(proc)
            if reader_task is not None:
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)
            return ToolResult(output=f"Error executing command: {e}", is_error=True)

        # 合并流输出，不再区分 stdout/stderr
        output = stdout.decode(errors="replace") if stdout else ""
        if discarded:
            output = (
                output.rstrip()
                + f"\n\n[output truncated: discarded {discarded:,} bytes]"
            )

        exit_code = proc.returncode or 0
        is_error = _interpret_exit_code(params.command, exit_code)
        if exit_code != 0:
            hint = _exit_code_hint(params.command, exit_code)
            if output:
                output = f"{output.rstrip()}\n\n{hint}"
            else:
                output = hint

        if not output:
            output = "(no output)"

        return ToolResult(output=output, is_error=is_error)

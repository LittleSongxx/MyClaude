from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from myclaude.agent import Agent
from myclaude.client import LLMClient
from myclaude.diagnostics import LSPDiagnostics
from myclaude.tools.base import ToolResult
from myclaude.tools.bash import Bash, Params as BashParams
from myclaude.tools.bash_tasks import BashOutput, BashStop, OutputParams, StopParams
from myclaude.tools.grep import Grep, Params as GrepParams
from myclaude.tools.process_manager import ProcessManager
from myclaude.tools.read_file import Params as ReadParams
from myclaude.tools.read_file import ReadFile
from myclaude.tools import ToolRegistry
from myclaude.tools.write_file import Params as WriteParams
from myclaude.tools.write_file import WriteFile


class _UnusedClient(LLMClient):
    async def stream(self, conversation, system="", tools=None):
        if False:
            yield None


def test_tool_result_keeps_legacy_constructor() -> None:
    result = ToolResult("ok", True)
    assert result.output == "ok"
    assert result.is_error is True
    assert result.artifact_path == ""


def test_agent_spills_medium_result_without_losing_tail(tmp_path: Path) -> None:
    agent = Agent(_UnusedClient(), ToolRegistry(), "anthropic", work_dir=str(tmp_path))
    text = "HEAD\n" + ("x" * 15_000) + "\nTAIL"
    result = ToolResult(output=text)

    preview = agent._prepare_tool_result("medium", result)

    assert result.truncated is True
    assert result.artifact_path
    assert Path(result.artifact_path).read_text() == text
    assert "HEAD" in preview and "TAIL" in preview


@pytest.mark.asyncio
async def test_read_returns_partial_view_and_cursor(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_text("\n".join(f"{index}:" + "x" * 250 for index in range(100)))
    tool = ReadFile()
    tool.work_dir = str(tmp_path)

    first = await tool.execute(ReadParams(file_path="large.txt"))

    assert first.truncated is True
    assert first.next_offset is not None
    assert "PARTIAL view" in first.output
    second = await tool.execute(
        ReadParams(file_path="large.txt", offset=first.next_offset, limit=100)
    )
    assert second.output.startswith(f"{first.next_offset + 1}\t")


@pytest.mark.asyncio
async def test_read_notebook_renders_cells(tmp_path: Path) -> None:
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": "# Title\n", "outputs": []},
            {
                "cell_type": "code",
                "source": "print(1)\n",
                "outputs": [{"data": {"text/plain": "1\n"}}],
            },
        ]
    }
    (tmp_path / "demo.ipynb").write_text(json.dumps(notebook))
    tool = ReadFile()
    tool.work_dir = str(tmp_path)

    result = await tool.execute(ReadParams(file_path="demo.ipynb"))

    assert "Cell 1 [markdown]" in result.output
    assert "print(1)" in result.output
    assert "1" in result.output
    assert result.metadata["content_kind"] == "notebook"


@pytest.mark.asyncio
async def test_read_image_returns_artifact_metadata(tmp_path: Path) -> None:
    image = tmp_path / "pixel.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    tool = ReadFile()
    tool.work_dir = str(tmp_path)

    result = await tool.execute(ReadParams(file_path="pixel.png"))

    assert result.artifact_path == str(image.resolve())
    assert result.mime_type == "image/png"
    assert result.metadata["content_kind"] == "image"


@pytest.mark.asyncio
async def test_grep_honors_gitignore_and_supports_modes(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "visible.txt").write_text("needle\nneedle two\n")
    (tmp_path / "ignored.txt").write_text("needle hidden\n")
    tool = Grep()
    tool.work_dir = str(tmp_path)

    content = await tool.execute(GrepParams(pattern="needle", path="."))
    files = await tool.execute(
        GrepParams(pattern="needle", path=".", output_mode="files_with_matches")
    )
    count = await tool.execute(
        GrepParams(pattern="needle", path=".", output_mode="count")
    )

    assert "visible.txt" in content.output
    assert "ignored.txt" not in content.output
    assert "visible.txt" in files.output
    assert "2" in count.output


@pytest.mark.asyncio
async def test_grep_paginates_results(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text("\n".join("match" for _ in range(20)))
    tool = Grep()
    tool.work_dir = str(tmp_path)

    result = await tool.execute(GrepParams(pattern="match", limit=5))

    assert result.truncated is True
    assert result.next_offset == 5
    assert "PARTIAL results" in result.output


@pytest.mark.asyncio
async def test_bash_large_output_is_preserved_as_artifact(tmp_path: Path) -> None:
    manager = ProcessManager()
    tool = Bash(manager)
    tool.work_dir = str(tmp_path)
    command = f"{sys.executable} -c \"print('A' * 40000)\""

    result = await tool.execute(BashParams(command=command))

    assert result.artifact_path
    assert result.truncated is True
    artifact = Path(result.artifact_path).read_text()
    assert len(artifact.strip()) == 40_000
    assert "middle output omitted" in result.output


@pytest.mark.asyncio
async def test_bash_carries_safe_working_directory(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    tool = Bash(ProcessManager())
    tool.work_dir = str(tmp_path)

    changed = await tool.execute(BashParams(command="cd sub"))
    current = await tool.execute(BashParams(command="pwd"))

    assert changed.is_error is False
    assert current.output.strip() == str((tmp_path / "sub").resolve())


@pytest.mark.asyncio
async def test_bash_timeout_moves_process_to_background(tmp_path: Path) -> None:
    manager = ProcessManager()
    bash = Bash(manager)
    output_tool = BashOutput(manager)
    bash.work_dir = output_tool.work_dir = str(tmp_path)
    command = (
        f"{sys.executable} -c \"import time; print('start', flush=True); "
        "time.sleep(1.3); print('done', flush=True)\""
    )

    result = await bash.execute(BashParams(command=command, timeout=1))
    task_id = str(result.metadata["task_id"])
    assert "moved to the background" in result.output
    await asyncio.sleep(0.5)
    output = await output_tool.execute(OutputParams(task_id=task_id))

    assert "done" in output.output
    assert output.metadata["status"] == "completed"


@pytest.mark.asyncio
async def test_background_process_can_be_stopped(tmp_path: Path) -> None:
    manager = ProcessManager()
    bash = Bash(manager)
    stop_tool = BashStop(manager)
    bash.work_dir = stop_tool.work_dir = str(tmp_path)
    command = f"{sys.executable} -c \"import time; time.sleep(30)\""

    result = await bash.execute(BashParams(command=command, run_in_background=True))
    task_id = str(result.metadata["task_id"])
    stopped = await stop_tool.execute(StopParams(task_id=task_id))

    assert stopped.is_error is False
    assert manager.get(task_id).status == "stopped"


@pytest.mark.asyncio
async def test_write_appends_syntax_diagnostics(tmp_path: Path) -> None:
    tool = WriteFile(diagnostics=LSPDiagnostics(timeout=0.2))
    tool.work_dir = str(tmp_path)

    result = await tool.execute(
        WriteParams(file_path="broken.py", content="def broken(:\n    pass\n")
    )

    assert result.is_error is False
    assert "Diagnostics after edit" in result.output
    assert "python-syntax" in result.output
    assert result.metadata["diagnostics"]

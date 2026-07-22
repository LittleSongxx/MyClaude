from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, Tool, ToolResult
from myclaude.tools.diff import build_diff
from myclaude.tools.file_io import atomic_write_text, locked_path

if TYPE_CHECKING:
    from myclaude.cache import FileCache
    from myclaude.diagnostics import LSPDiagnostics
    from myclaude.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to edit")
    old_string: str = Field(description="The exact string to find and replace (must be unique in file)")
    new_string: str = Field(description="The replacement string")


class EditFile(Tool):
    name = "EditFile"
    description = (
        "Replace an exact string in a file. The old_string must appear exactly once in the file.\n"
        "You MUST read the file with ReadFile before editing. This tool will fail otherwise."
    )
    params_model = Params
    category = "write"

    def permission_scope(self, arguments: dict[str, object]) -> PermissionScope:
        path = str(arguments.get("file_path", ""))
        return PermissionScope(content=path, path=path)


    def __init__(self, file_cache: FileCache | None = None, file_history: Any = None, file_state_cache: FileStateCache | None = None, diagnostics: LSPDiagnostics | None = None) -> None:
        self._cache = file_cache
        self.file_history = file_history
        self._state_cache = file_state_cache
        self._diagnostics = diagnostics


    async def execute(self, params: Params) -> ToolResult:
        path = self.resolve_path(params.file_path)

        def edit() -> ToolResult:
            with locked_path(path):
                if not path.exists():
                    return ToolResult(
                        output=f"Error: file not found: {params.file_path}",
                        is_error=True,
                    )

                resolved = str(path.resolve())
                if self._state_cache is not None:
                    ok, err_msg = self._state_cache.check(resolved)
                    if not ok:
                        return ToolResult(output=err_msg, is_error=True)

                try:
                    content = path.read_text(encoding="utf-8")
                except Exception as e:
                    return ToolResult(
                        output=f"Error reading file: {e}", is_error=True
                    )

                count = content.count(params.old_string)
                if count == 0:
                    return ToolResult(
                        output="Error: old_string not found in file", is_error=True
                    )
                if count > 1:
                    return ToolResult(
                        output=(
                            f"Error: old_string found {count} times, must be unique"
                        ),
                        is_error=True,
                    )

                new_content = content.replace(
                    params.old_string, params.new_string, 1
                )
                try:
                    if self.file_history is not None:
                        self.file_history.track_edit(str(path))
                    atomic_write_text(path, new_content)
                    if self._cache is not None:
                        self._cache.invalidate(resolved)
                    if self._state_cache is not None:
                        self._state_cache.update(resolved)
                except Exception as e:
                    return ToolResult(
                        output=f"Error writing file: {e}", is_error=True
                    )

                diff = build_diff(content, new_content)
                addition_word = "addition" if diff.additions == 1 else "additions"
                removal_word = "removal" if diff.removals == 1 else "removals"
                summary = (
                    f"Updated {params.file_path} with {diff.additions} {addition_word} "
                    f"and {diff.removals} {removal_word}"
                )
                return ToolResult(output=f"{summary}\n{diff.text}")

        result = await asyncio.to_thread(edit)
        from myclaude.diagnostics import append_post_edit_diagnostics
        return await append_post_edit_diagnostics(
            result,
            path,
            self._diagnostics,
            workspace=Path(self.work_dir or path.parent),
        )

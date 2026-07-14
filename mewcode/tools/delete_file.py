from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.file_io import locked_path

if TYPE_CHECKING:
    from mewcode.cache import FileCache
    from mewcode.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Absolute or relative path of the file to delete")


class DeleteFile(Tool):
    """Delete one previously-read file without exposing a shell escape hatch."""

    name = "DeleteFile"
    description = "Delete a single file. Directories are refused."
    params_model = Params
    category = "write"

    def __init__(
        self,
        file_cache: FileCache | None = None,
        file_history: Any = None,
        file_state_cache: FileStateCache | None = None,
    ) -> None:
        self._cache = file_cache
        self.file_history = file_history
        self._state_cache = file_state_cache

    async def execute(self, params: Params) -> ToolResult:
        path = self.resolve_path(params.file_path)

        def remove() -> ToolResult:
            with locked_path(path):
                if not path.exists() and not path.is_symlink():
                    return ToolResult(
                        output=f"Error: file not found: {params.file_path}",
                        is_error=True,
                    )
                if path.is_dir() and not path.is_symlink():
                    return ToolResult(
                        output="Error: DeleteFile refuses to delete directories",
                        is_error=True,
                    )

                resolved = str(path.resolve())
                if self._state_cache is not None:
                    ok, error = self._state_cache.check(resolved)
                    if not ok:
                        return ToolResult(output=error, is_error=True)
                if self.file_history is not None:
                    self.file_history.track_edit(resolved)
                try:
                    path.unlink()
                except OSError as exc:
                    return ToolResult(
                        output=f"Error deleting file: {exc}",
                        is_error=True,
                    )
                if self._cache is not None:
                    self._cache.invalidate(resolved)
                if self._state_cache is not None:
                    self._state_cache.invalidate(resolved)
                return ToolResult(output=f"Deleted {params.file_path}")

        return await asyncio.to_thread(remove)

# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, Tool, ToolResult

if TYPE_CHECKING:
    from myclaude.cache import FileCache
    from myclaude.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file to read")
    offset: int = Field(default=0, ge=0, description="Line offset to start reading from (0-based)")
    limit: int = Field(default=2000, ge=1, le=10_000, description="Maximum number of lines to read")


class ReadFile(Tool):
    name = "ReadFile"
    description = "Read a file and return its contents with line numbers."
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    def permission_scope(self, arguments: dict[str, object]) -> PermissionScope:
        path = str(arguments.get("file_path", ""))
        return PermissionScope(content=path, path=path)


    def __init__(self, file_cache: FileCache | None = None, file_state_cache: FileStateCache | None = None) -> None:
        self._cache = file_cache
        self._state_cache = file_state_cache


    async def execute(self, params: Params) -> ToolResult:
        path = self.resolve_path(params.file_path)

        def read() -> ToolResult:
            if not path.exists():
                return ToolResult(
                    output=f"Error: file not found: {params.file_path}",
                    is_error=True,
                )
            if not path.is_file():
                return ToolResult(
                    output=f"Error: not a file: {params.file_path}", is_error=True
                )

            resolved = str(path.resolve())
            try:
                before = path.stat()
                text = None
                if self._cache is not None:
                    text = self._cache.get(
                        resolved,
                        mtime_ns=before.st_mtime_ns,
                        size=before.st_size,
                    )
                if text is None:
                    text = path.read_text(encoding="utf-8")
                    after = path.stat()
                    # Retry once if the file changed while it was being read.
                    if (
                        before.st_mtime_ns != after.st_mtime_ns
                        or before.st_size != after.st_size
                    ):
                        text = path.read_text(encoding="utf-8")
                        after = path.stat()
                    if self._cache is not None:
                        self._cache.put(
                            resolved,
                            text,
                            mtime_ns=after.st_mtime_ns,
                            size=after.st_size,
                        )
                else:
                    after = before
            except Exception as e:
                return ToolResult(output=f"Error reading file: {e}", is_error=True)

            if self._state_cache is not None:
                self._state_cache.record(resolved, text, after.st_mtime_ns)

            lines = text.splitlines()
            selected = lines[params.offset : params.offset + params.limit]
            numbered = [
                f"{i + params.offset + 1}\t{line}"
                for i, line in enumerate(selected)
            ]
            return ToolResult(output="\n".join(numbered))

        return await asyncio.to_thread(read)

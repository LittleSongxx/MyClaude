from __future__ import annotations

import asyncio
import json
import mimetypes
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, Tool, ToolResult

if TYPE_CHECKING:
    from myclaude.cache import FileCache
    from myclaude.tools.file_state_cache import FileStateCache

READ_CHAR_BUDGET = 9_000
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


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
            mime_type = mimetypes.guess_type(path.name)[0] or "text/plain"
            if path.suffix.lower() in IMAGE_SUFFIXES:
                size = path.stat().st_size
                return ToolResult(
                    output=(
                        f"Image file: {resolved}\n"
                        f"MIME type: {mime_type}\n"
                        f"Size: {size:,} bytes\n"
                        "The file is available as an artifact for a vision-capable client."
                    ),
                    artifact_path=resolved,
                    mime_type=mime_type,
                    total_bytes=size,
                    metadata={"content_kind": "image"},
                )
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

            rendered_text = text
            if path.suffix.lower() == ".ipynb":
                try:
                    notebook = json.loads(text)
                    chunks: list[str] = []

                    def append_text(value: object) -> None:
                        if isinstance(value, str):
                            chunks.append(value)
                        elif isinstance(value, list):
                            chunks.extend(str(item) for item in value)

                    for index, cell in enumerate(notebook.get("cells", []), 1):
                        kind = cell.get("cell_type", "unknown")
                        chunks.append(f"# Cell {index} [{kind}]")
                        append_text(cell.get("source", []))
                        for output in cell.get("outputs", []):
                            chunks.append("# Output")
                            append_text(output.get("text", []))
                            data = output.get("data", {})
                            if isinstance(data, dict):
                                append_text(data.get("text/plain", []))
                    rendered_text = "".join(chunks)
                    mime_type = "application/x-ipynb+json"
                except (TypeError, ValueError, json.JSONDecodeError):
                    return ToolResult(
                        output=f"Error: invalid Jupyter notebook: {params.file_path}",
                        is_error=True,
                    )

            lines = rendered_text.splitlines()
            total_lines = len(lines)
            if params.offset >= total_lines and total_lines:
                return ToolResult(
                    output=(
                        f"Offset {params.offset} is past the end of the file "
                        f"({total_lines} lines)."
                    ),
                    total_bytes=len(text.encode("utf-8")),
                    total_lines=total_lines,
                    mime_type=mime_type,
                )
            if not lines:
                return ToolResult(
                    output="(file exists but is empty)",
                    total_bytes=0,
                    total_lines=0,
                    mime_type=mime_type,
                )

            selected = lines[params.offset : params.offset + params.limit]
            numbered: list[str] = []
            used = 0
            for index, line in enumerate(selected, params.offset + 1):
                rendered = f"{index}\t{line}"
                if numbered and used + len(rendered) + 1 > READ_CHAR_BUDGET:
                    break
                if not numbered and len(rendered) > READ_CHAR_BUDGET:
                    rendered = rendered[:READ_CHAR_BUDGET]
                numbered.append(rendered)
                used += len(rendered) + 1

            consumed = len(numbered)
            next_offset = params.offset + consumed
            partial = next_offset < total_lines and consumed < params.limit
            output = "\n".join(numbered)
            if next_offset < total_lines:
                output += (
                    f"\n\n[PARTIAL view: lines {params.offset + 1}-{next_offset} "
                    f"of {total_lines}. Continue with offset={next_offset} "
                    f"and limit={params.limit}.]"
                )
            return ToolResult(
                output=output,
                mime_type=mime_type,
                truncated=partial,
                total_bytes=len(text.encode("utf-8")),
                total_lines=total_lines,
                next_offset=next_offset if next_offset < total_lines else None,
                metadata={"content_kind": "notebook" if path.suffix.lower() == ".ipynb" else "text"},
            )

        return await asyncio.to_thread(read)

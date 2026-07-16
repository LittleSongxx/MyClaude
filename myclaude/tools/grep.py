from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, SKIP_DIRS, Tool, ToolResult

MAX_MATCHES = 1_000
MAX_LINE_CHARS = 1_000


class Params(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="Base directory to search from")
    include: str = Field(default="", description="Glob filter for filenames (e.g. '*.py')")


class Grep(Tool):
    name = "Grep"
    description = "Search file contents using a regex pattern, returning file:line:content matches."
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    def permission_scope(self, arguments: dict[str, object]) -> PermissionScope:
        return PermissionScope(
            content=str(arguments.get("pattern", "")),
            path=str(arguments.get("path", ".")),
        )


    async def execute(self, params: Params) -> ToolResult:
        base = self.resolve_path(params.path)

        def search() -> ToolResult:
            if not base.exists():
                return ToolResult(
                    output=f"Error: path not found: {params.path}", is_error=True
                )

            try:
                regex = re.compile(params.pattern)
            except re.error as e:
                return ToolResult(output=f"Error: invalid regex: {e}", is_error=True)

            glob_pattern = params.include if params.include else "**/*"
            if not glob_pattern.startswith("**/"):
                glob_pattern = "**/" + glob_pattern

            results: list[str] = []
            for file_path in base.glob(glob_pattern):
                if not file_path.is_file():
                    continue
                if any(part in SKIP_DIRS for part in file_path.parts):
                    continue
                try:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                except (OSError, UnicodeDecodeError):
                    continue
                for line_num, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        rel = file_path.relative_to(base)
                        preview = line[:MAX_LINE_CHARS]
                        results.append(f"{rel}:{line_num}:{preview}")
                        if len(results) >= MAX_MATCHES:
                            results.append(
                                f"[results truncated after {MAX_MATCHES:,} matches]"
                            )
                            return ToolResult(output="\n".join(results))

            if not results:
                return ToolResult(output="No matches found.")
            return ToolResult(output="\n".join(results))

        return await asyncio.to_thread(search)

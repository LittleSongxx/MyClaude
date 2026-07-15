# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

import asyncio
import heapq

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, SKIP_DIRS, Tool, ToolResult

MAX_MATCHES = 2_000


class Params(BaseModel):
    pattern: str = Field(description="Glob pattern to match (e.g. '**/*.py')")
    path: str = Field(default=".", description="Base directory to search from")


class Glob(Tool):
    name = "Glob"
    description = "Find files matching a glob pattern, returning relative paths."
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

        def find() -> ToolResult:
            if not base.exists():
                return ToolResult(
                    output=f"Error: path not found: {params.path}", is_error=True
                )

            try:
                newest: list[tuple[float, str]] = []
                total = 0
                for path in base.glob(params.pattern):
                    if not path.is_file() or any(
                        part in SKIP_DIRS for part in path.parts
                    ):
                        continue
                    total += 1
                    entry = (path.stat().st_mtime, str(path.relative_to(base)))
                    if len(newest) < MAX_MATCHES:
                        heapq.heappush(newest, entry)
                    elif entry > newest[0]:
                        heapq.heapreplace(newest, entry)
                newest.sort(reverse=True)
                matches = [relative for _, relative in newest]
            except Exception as e:
                return ToolResult(output=f"Error: {e}", is_error=True)

            if not matches:
                return ToolResult(output="No files matched the pattern.")
            if total > MAX_MATCHES:
                matches.append(
                    f"[results truncated: showing newest {MAX_MATCHES:,} of {total:,} files]"
                )
            return ToolResult(output="\n".join(matches))

        return await asyncio.to_thread(find)

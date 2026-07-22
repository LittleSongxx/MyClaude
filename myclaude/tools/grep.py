from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, SKIP_DIRS, Tool, ToolResult

MAX_MATCHES = 1_000
MAX_LINE_CHARS = 1_000
GREP_CHAR_BUDGET = 9_000


class Params(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="Base file or directory to search")
    include: str = Field(default="", description="Glob filter such as '*.py'")
    output_mode: Literal["content", "files_with_matches", "count"] = "content"
    offset: int = Field(default=0, ge=0, description="Result offset for pagination")
    limit: int = Field(default=100, ge=1, le=MAX_MATCHES, description="Maximum results")
    case_sensitive: bool = True
    context: int = Field(default=0, ge=0, le=20, description="Context lines around matches")


class Grep(Tool):
    name = "Grep"
    description = (
        "Search file contents with ripgrep-compatible regex. Honors .gitignore and "
        "supports content, file-list, count, context, and paginated result modes."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    def permission_scope(self, arguments: dict[str, object]) -> PermissionScope:
        return PermissionScope(
            content=str(arguments.get("pattern", "")),
            path=str(arguments.get("path", ".")),
        )

    @staticmethod
    def _page(lines: list[str], params: Params) -> ToolResult:
        total = len(lines)
        page: list[str] = []
        used = 0
        for line in lines[params.offset : params.offset + params.limit]:
            rendered = line[:MAX_LINE_CHARS]
            if page and used + len(rendered) + 1 > GREP_CHAR_BUDGET:
                break
            page.append(rendered)
            used += len(rendered) + 1
        next_offset = params.offset + len(page)
        output = "\n".join(page) if page else "No matches found."
        if next_offset < total:
            output += (
                f"\n\n[PARTIAL results: {params.offset + 1}-{next_offset} of {total}. "
                f"Continue with offset={next_offset}.]"
            )
        return ToolResult(
            output=output,
            truncated=next_offset < total,
            total_bytes=sum(len(line.encode("utf-8")) + 1 for line in lines),
            total_lines=total,
            next_offset=next_offset if next_offset < total else None,
            metadata={"output_mode": params.output_mode, "engine": "ripgrep"},
        )

    def _search_with_rg(self, base: Path, params: Params) -> ToolResult:
        args = [
            "rg",
            "--color=never",
            "--no-heading",
            "--no-require-git",
            "--max-columns=1000",
            "--max-columns-preview",
        ]
        if params.output_mode == "content":
            args.extend(["--line-number", "--with-filename"])
            if params.context:
                args.extend(["--context", str(params.context)])
        elif params.output_mode == "files_with_matches":
            args.append("--files-with-matches")
        else:
            args.append("--count-matches")
        if not params.case_sensitive:
            args.append("--ignore-case")
        if params.include:
            args.extend(["--glob", params.include])
        target = params.path if self.work_dir and not Path(params.path).is_absolute() else str(base)
        args.extend(["--", params.pattern, target])
        try:
            proc = subprocess.run(
                args,
                cwd=self.work_dir,
                capture_output=True,
                text=True,
                timeout=60.0,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(output="Error: ripgrep timed out after 60s", is_error=True)
        except OSError as error:
            return ToolResult(output=f"Error starting ripgrep: {error}", is_error=True)
        if proc.returncode == 1:
            return ToolResult(
                output="No matches found.",
                metadata={"output_mode": params.output_mode, "engine": "ripgrep"},
            )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "unknown ripgrep error").strip()
            return ToolResult(output=f"Error: {detail}", is_error=True)
        lines = proc.stdout.splitlines()
        return self._page(lines, params)

    def _search_fallback(self, base: Path, params: Params) -> ToolResult:
        try:
            regex = re.compile(params.pattern, 0 if params.case_sensitive else re.IGNORECASE)
        except re.error as error:
            return ToolResult(output=f"Error: invalid regex: {error}", is_error=True)
        glob_pattern = params.include or "**/*"
        if not glob_pattern.startswith("**/"):
            glob_pattern = "**/" + glob_pattern
        matches: list[str] = []
        files: dict[str, int] = {}
        candidates = [base] if base.is_file() else base.glob(glob_pattern)
        for file_path in candidates:
            if not file_path.is_file() or any(part in SKIP_DIRS for part in file_path.parts):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            relative = str(file_path.relative_to(base if base.is_dir() else base.parent))
            for line_number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    files[relative] = files.get(relative, 0) + 1
                    matches.append(f"{relative}:{line_number}:{line}")
        if params.output_mode == "files_with_matches":
            matches = list(files)
        elif params.output_mode == "count":
            matches = [f"{name}:{count}" for name, count in files.items()]
        result = self._page(matches, params)
        result.metadata["engine"] = "python-fallback"
        return result

    async def execute(self, params: Params) -> ToolResult:
        base = self.resolve_path(params.path)

        def search() -> ToolResult:
            if not base.exists():
                return ToolResult(output=f"Error: path not found: {params.path}", is_error=True)
            if shutil.which("rg"):
                return self._search_with_rg(base, params)
            return self._search_fallback(base, params)

        return await asyncio.to_thread(search)

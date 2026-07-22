from __future__ import annotations

import os
import fnmatch
from dataclasses import dataclass
from pathlib import Path

import yaml

MAX_INCLUDE_DEPTH = 5


# ---------------------------------------------------------------------------
# @include 指令格式
# ---------------------------------------------------------------------------
# 支持以下格式：
#   @./relative/path  @../relative/path  @~/home/path  @/absolute/path
# 其他 @-token（如 @username）被忽略，不视为 include 指令。
# 旧的 "@include path" 格式仍保留兼容。


def _parse_include(trimmed: str) -> str:
    """解析一行文本，提取 @include 路径。

    支持 @./path @../path @~/path @/path 语法，
    以及旧格式 "@include path"。返回空字符串表示该行不是 include 指令。
    """
    # 旧格式兼容：@include <path>
    if trimmed.startswith("@include "):
        return trimmed[len("@include ") :].strip()

    # 新格式：@./path, @../path, @~/path, @/abs/path
    if not trimmed.startswith("@") or trimmed.startswith("@@"):
        return ""
    rest = trimmed[1:]  # 去掉 @
    if not rest:
        return ""
    # 包含空白字符则不是 include 指令（如 @username 等普通文本）
    if " " in rest or "\t" in rest:
        return ""
    if (
        rest.startswith("./")
        or rest.startswith("../")
        or rest.startswith("~/")
        or rest.startswith("/")
    ):
        return rest
    return ""


def _resolve_include(path: str, base_dir: Path) -> Path:
    """将 include 路径解析为绝对路径。

    ~/ 展开为 home，相对路径基于 base_dir 解析。
    """
    if path.startswith("~/"):
        return Path.home() / path[2:]
    if os.path.isabs(path):
        return Path(path)
    return base_dir / path


def process_includes(
    content: str,
    base_dir: Path,
    project_root: Path,
    depth: int = 0,
    seen: set[str] | None = None,
) -> str:
    """展开 @include 指令。

    - 循环检测：通过 seen 集合记录已包含文件的绝对路径，防止 A→B→A 无限递归
    - 代码块跳过：``` 围栏代码块内的 @include 不展开
    - 深度限制：最多递归 MAX_INCLUDE_DEPTH 层
    """
    if depth > MAX_INCLUDE_DEPTH:
        return content

    if seen is None:
        seen = set()

    lines = content.split("\n")
    result: list[str] = []
    in_code = False  # 追踪是否处于 ``` 围栏代码块内

    for line in lines:
        stripped = line.strip()

        # 检测围栏代码块边界
        if stripped.startswith("```"):
            in_code = not in_code
            result.append(line)
            continue

        # 代码块内不展开 include 指令
        if not in_code:
            include_path = _parse_include(stripped)
            if include_path:
                resolved = _resolve_include(include_path, base_dir)
                try:
                    abs_str = str(resolved.resolve())
                except OSError:
                    result.append(line)
                    continue

                # 循环检测：已包含过的文件跳过
                if abs_str in seen:
                    result.append(line)
                    continue

                if not resolved.exists() or not resolved.is_file():
                    result.append("<!-- @include skipped: file not found -->")
                    continue

                try:
                    included = resolved.read_text(encoding="utf-8")
                except OSError:
                    result.append(line)
                    continue

                seen.add(abs_str)
                result.append(f"<!-- included from {include_path} -->")
                result.append(
                    process_includes(
                        included, resolved.parent, project_root, depth + 1, seen
                    )
                )
                continue

        result.append(line)

    return "\n".join(result)


def _find_git_root(start: Path) -> Path | None:
    """从 start 向上查找 .git 目录，返回 git 仓库根目录。"""
    cur = start.resolve()
    while True:
        if (cur / ".git").exists():
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def _project_instruction_dirs(work_dir: Path) -> list[Path]:
    """返回从 git root 到 work_dir 的所有目录。

    如果 work_dir 不在 git 仓库内，只返回 [work_dir]。
    """
    abs_dir = work_dir.resolve()
    root = _find_git_root(abs_dir)
    if root is None:
        return [abs_dir]

    dirs: list[Path] = []
    cur = abs_dir
    while True:
        dirs.insert(0, cur)
        if cur == root:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return dirs


def load_instructions(project_root: str, *, include_project: bool = True) -> str:
    """发现并拼接项目和用户指令文件。

    发现顺序（低优先级在前，高优先级在后）：
    1. 用户全局：~/.myclaude/MYCLAUDE.md, ~/.myclaude/AGENTS.md
    2. 项目目录链：从 git root 到 workDir，每个目录的 MYCLAUDE.md 和 AGENTS.md
    3. workDir/.myclaude/INSTRUCTIONS.md（遗留格式）
    4. workDir/MYCLAUDE.local.md（本地覆盖）
    """
    root = Path(project_root).resolve()
    home = Path.home()
    seen: set[str] = set()  # 用于文件去重
    sources: list[tuple[str, str]] = []  # (label, content)

    def _add(path: Path) -> None:
        """尝试加载一个指令文件，处理 include 展开。"""
        try:
            abs_path = path.resolve()
            abs_str = str(abs_path)
        except OSError:
            return
        if abs_str in seen:
            return
        if not abs_path.exists() or not abs_path.is_file():
            return
        try:
            data = abs_path.read_text(encoding="utf-8")
        except OSError:
            return
        seen.add(abs_str)
        # 每个文件独立的 include seen 集合，但共享全局文件去重
        include_seen: set[str] = {abs_str}
        content = process_includes(data, abs_path.parent, root, 0, include_seen)

        # 生成标签：尽量用相对路径
        try:
            label = str(abs_path.relative_to(root))
        except ValueError:
            label = abs_str
        sources.append((label, content.rstrip("\n")))

    # 1. 用户全局
    _add(home / ".myclaude" / "MYCLAUDE.md")
    _add(home / ".myclaude" / "AGENTS.md")

    if include_project:
        # 2. 项目目录链
        for d in _project_instruction_dirs(root):
            _add(d / "MYCLAUDE.md")
            _add(d / "AGENTS.md")

        # 3. 遗留格式
        _add(root / ".myclaude" / "INSTRUCTIONS.md")

        # 4. 本地覆盖
        _add(root / "MYCLAUDE.local.md")

    if not sources:
        return ""

    parts = [f"Contents of {label}:\n\n{content}" for label, content in sources]
    return "\n\n---\n\n".join(parts)


@dataclass(frozen=True)
class ScopedInstruction:
    path: Path
    patterns: tuple[str, ...]
    content: str


class InstructionResolver:
    """Resolve global and path-scoped instructions with one-time lazy loading."""

    def __init__(self, work_dir: str, *, include_project: bool = True) -> None:
        self.work_dir = Path(work_dir).expanduser().resolve()
        self.project_root = _find_git_root(self.work_dir) or self.work_dir
        self.include_project = include_project
        self._loaded_paths: set[str] = set()
        self._loaded_labels: list[str] = []
        self._scoped: list[ScopedInstruction] = []
        self._pending_labels: list[str] = []

        initial_parts: list[str] = []
        base = load_instructions(str(self.work_dir), include_project=include_project)
        if base:
            initial_parts.append(base)
        self._mark_initial_instruction_files()
        if include_project:
            initial_parts.extend(self._scan_rules())
        self.initial_content = "\n\n---\n\n".join(initial_parts)

    def _mark_initial_instruction_files(self) -> None:
        candidates = [
            Path.home() / ".myclaude" / "MYCLAUDE.md",
            Path.home() / ".myclaude" / "AGENTS.md",
        ]
        if self.include_project:
            for directory in _project_instruction_dirs(self.work_dir):
                candidates.extend(
                    [directory / "MYCLAUDE.md", directory / "AGENTS.md"]
                )
            candidates.extend(
                [
                    self.work_dir / ".myclaude" / "INSTRUCTIONS.md",
                    self.work_dir / "MYCLAUDE.local.md",
                ]
            )
        for path in candidates:
            if path.is_file():
                self._mark_loaded(path)

    def _scan_rules(self) -> list[str]:
        initial: list[str] = []
        rules_dir = self.project_root / ".myclaude" / "rules"
        if not rules_dir.is_dir():
            return initial
        for path in sorted(rules_dir.rglob("*.md")):
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            patterns, body = self._parse_rule(raw)
            content = process_includes(
                body, path.parent, self.project_root, seen={str(path.resolve())}
            ).rstrip("\n")
            rendered = self._render(path, content)
            if patterns:
                self._scoped.append(
                    ScopedInstruction(path=path.resolve(), patterns=patterns, content=rendered)
                )
                self._pending_labels.append(self._label(path))
            else:
                initial.append(rendered)
                self._mark_loaded(path)
        return initial

    @staticmethod
    def _parse_rule(raw: str) -> tuple[tuple[str, ...], str]:
        stripped = raw.lstrip()
        if not stripped.startswith("---"):
            return (), raw
        end = stripped.find("---", 3)
        if end < 0:
            return (), raw
        try:
            meta = yaml.safe_load(stripped[3:end]) or {}
        except yaml.YAMLError:
            return (), raw
        if not isinstance(meta, dict):
            return (), stripped[end + 3 :].lstrip("\n")
        paths = meta.get("paths", ())
        if isinstance(paths, str):
            patterns = (paths,)
        elif isinstance(paths, list):
            patterns = tuple(str(item) for item in paths if str(item).strip())
        else:
            patterns = ()
        return patterns, stripped[end + 3 :].lstrip("\n")

    def on_file_access(self, value: str | Path) -> str:
        """Return instructions newly made relevant by a file access."""
        if not self.include_project:
            return ""
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.work_dir / path
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(self.project_root).as_posix()
        except (OSError, ValueError):
            return ""

        loaded: list[str] = []
        target_dir = resolved if resolved.is_dir() else resolved.parent
        try:
            rel_dir = target_dir.relative_to(self.project_root)
        except ValueError:
            return ""
        current = self.project_root
        directories = [current]
        for part in rel_dir.parts:
            current = current / part
            directories.append(current)
        for directory in directories:
            for name in ("MYCLAUDE.md", "AGENTS.md"):
                instruction = directory / name
                if instruction.is_file() and not self._is_loaded(instruction):
                    rendered = self._read_instruction(instruction)
                    if rendered:
                        loaded.append(rendered)
                    self._mark_loaded(instruction)

        for rule in self._scoped:
            if self._is_loaded(rule.path):
                continue
            if any(self._matches(relative, pattern) for pattern in rule.patterns):
                loaded.append(rule.content)
                self._mark_loaded(rule.path)
                label = self._label(rule.path)
                if label in self._pending_labels:
                    self._pending_labels.remove(label)
        return "\n\n---\n\n".join(loaded)

    def _read_instruction(self, path: Path) -> str:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        content = process_includes(
            raw, path.parent, self.project_root, seen={str(path.resolve())}
        ).rstrip("\n")
        return self._render(path, content)

    @staticmethod
    def _matches(relative: str, pattern: str) -> bool:
        normalized = pattern.replace("\\", "/").lstrip("./")
        candidates = {normalized}
        if "**/" in normalized:
            candidates.add(normalized.replace("**/", ""))
        return any(
            fnmatch.fnmatchcase(relative, candidate)
            or Path(relative).match(candidate)
            for candidate in candidates
        )

    def _label(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            return str(path.resolve())

    def _render(self, path: Path, content: str) -> str:
        return f"Contents of {self._label(path)}:\n\n{content}"

    def _is_loaded(self, path: Path) -> bool:
        return str(path.resolve()) in self._loaded_paths

    def _mark_loaded(self, path: Path) -> None:
        key = str(path.resolve())
        if key in self._loaded_paths:
            return
        self._loaded_paths.add(key)
        self._loaded_labels.append(self._label(path))

    def diagnostics(self) -> dict[str, list[str]]:
        return {
            "loaded": list(self._loaded_labels),
            "pending_path_rules": list(self._pending_labels),
        }

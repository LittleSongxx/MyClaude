from __future__ import annotations

import tempfile
from pathlib import Path


class PathSandbox:

    # 默认禁写路径：这些文件包含敏感配置，不允许 Agent 直接修改
    _DEFAULT_DENY_WRITE: list[str] = [
        ".myclaude/config.yaml",
        ".myclaude/config.local.yaml",
        ".myclaude/permissions.yaml",
        ".myclaude/permissions.local.yaml",
        ".myclaude/skills/",
    ]

    def __init__(
        self,
        project_root: str,
        extra_allowed: list[str] | None = None,
        deny_write: list[str] | None = None,
        write_allowed: list[str] | None = None,
    ) -> None:
        self._extra_allowed = list(extra_allowed or [])
        if deny_write is None:
            user_state = Path.home() / ".myclaude"
            self._deny_write_config = [
                *self._DEFAULT_DENY_WRITE,
                str(user_state / "config.yaml"),
                str(user_state / "permissions.yaml"),
                str(user_state / "skills"),
                str(user_state / "trusted_workspaces.json"),
            ]
        else:
            self._deny_write_config = list(deny_write)
        self._write_allowed_config = (
            list(write_allowed) if write_allowed is not None else None
        )
        self._allowed_roots: list[Path] = []
        self._deny_write: list[Path] = []
        self._write_allowed: list[Path] | None = None
        self.set_project_root(project_root)

    def set_project_root(self, project_root: str) -> None:
        """Move the sandbox boundary when the active worktree changes."""
        root = Path(project_root).expanduser().resolve()
        self._allowed_roots = [root, Path(tempfile.gettempdir()).resolve()]
        for value in self._extra_allowed:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = root / path
            self._allowed_roots.append(path.resolve())

        self._deny_write = []
        for dp in self._deny_write_config:
            dp_path = Path(dp)
            if not dp_path.is_absolute():
                dp_path = root / dp_path
            self._deny_write.append(dp_path.resolve())

        if self._write_allowed_config is None:
            self._write_allowed = None
        else:
            self._write_allowed = []
            for value in self._write_allowed_config:
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = root / path
                self._write_allowed.append(path.resolve())


    @property
    def project_root(self) -> Path:
        return self._allowed_roots[0]


    def _is_deny_write(self, real_path: Path) -> bool:
        """检查路径是否命中禁写列表。

        支持目录前缀匹配：如果禁写项以 / 结尾或本身是目录，
        则该目录下的所有文件都被禁止写入。
        """
        for deny_path in self._deny_write:
            # 精确匹配
            if real_path == deny_path:
                return True
            # 目录前缀匹配
            try:
                real_path.relative_to(deny_path)
                return True
            except ValueError:
                continue
        return False

    def is_write_denied(self, path: str) -> bool:
        """Return whether a path hits a hard write-deny entry.

        This is deliberately separate from the normal allowed-root decision so
        permission bypass can allow an explicitly requested outside path while
        never allowing the agent to rewrite its own security configuration.
        """

        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.project_root / p
        try:
            real_path = p.resolve(strict=False)
        except OSError:
            return True
        return self._is_deny_write(real_path)


    def check(self, path: str, *, write: bool = False) -> tuple[bool, str]:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.project_root / p
        abs_path = p.absolute()

        try:
            real_path = abs_path.resolve(strict=True)
        except OSError:
            ancestor = abs_path
            while not ancestor.exists():
                parent = ancestor.parent
                if parent == ancestor:
                    return False, f"无法解析路径: {path}"
                ancestor = parent
            try:
                resolved_ancestor = ancestor.resolve(strict=True)
            except OSError:
                return False, f"无法解析路径: {path}"
            real_path = resolved_ancestor / abs_path.relative_to(ancestor)

        # Read access remains available for configuration inspection.  These
        # lists are write boundaries, not blanket path denials.
        if write and self._is_deny_write(real_path):
            return False, f"路径 {path} 在禁写列表中"

        if write and self._write_allowed is not None:
            if not any(
                _is_within(real_path, root) for root in self._write_allowed
            ):
                return False, f"路径 {path} 超出允许写入范围"

        for root in self._allowed_roots:
            try:
                real_path.relative_to(root)
                return True, ""
            except ValueError:
                continue

        return False, f"路径 {path} 超出沙箱范围"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

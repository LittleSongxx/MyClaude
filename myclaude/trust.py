from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from myclaude.tools.file_io import atomic_write_text


TRUST_SCHEMA_VERSION = 1
TRUST_FILENAME = "trusted_workspaces.json"


def resolve_workspace_root(path: str | Path) -> Path:
    """Resolve a stable repository root without invoking project code."""

    current = Path(path).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


@dataclass(frozen=True)
class WorkspaceTrust:
    root: Path
    trusted: bool


class WorkspaceTrustManager:
    def __init__(self, home: Path | None = None) -> None:
        self._home = (home or Path.home()).expanduser().resolve()
        self.path = self._home / ".myclaude" / TRUST_FILENAME

    def _load(self) -> set[str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(raw, dict):
            return set()
        version = raw.get("schema_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != TRUST_SCHEMA_VERSION
        ):
            return set()
        values = raw.get("trusted", [])
        if not isinstance(values, list):
            return set()
        return {value for value in values if isinstance(value, str)}

    def _save(self, trusted: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": TRUST_SCHEMA_VERSION,
            "trusted": sorted(trusted),
        }
        atomic_write_text(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def status(self, path: str | Path) -> WorkspaceTrust:
        root = resolve_workspace_root(path)
        return WorkspaceTrust(root=root, trusted=str(root) in self._load())

    def is_trusted(self, path: str | Path) -> bool:
        return self.status(path).trusted

    def trust(self, path: str | Path) -> Path:
        root = resolve_workspace_root(path)
        trusted = self._load()
        trusted.add(str(root))
        self._save(trusted)
        return root

    def revoke(self, path: str | Path) -> Path:
        root = resolve_workspace_root(path)
        trusted = self._load()
        trusted.discard(str(root))
        self._save(trusted)
        return root

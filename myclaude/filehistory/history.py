from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_SNAPSHOTS = 100
MANIFEST_VERSION = 1


@dataclass
class Backup:
    backup_path: str
    version: int
    timestamp: float
    existed: bool = True


@dataclass
class Snapshot:
    message_index: int
    user_text: str
    backups: dict[str, Backup] = field(default_factory=dict)
    timestamp: float = 0.0


class FileHistory:
    """Persistent pre-edit snapshots for session-scoped file rewind."""

    def __init__(self, base_dir: str, session_id: str) -> None:
        self._session_dir = Path(base_dir) / ".myclaude" / "file-history" / session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir.chmod(0o700)
        self._manifest_path = self._session_dir / "manifest.json"
        self._tracked: dict[str, int] = {}
        self._version_existed: dict[str, dict[int, bool]] = {}
        self._snapshots: list[Snapshot] = []
        self._lock = threading.Lock()
        self._load_manifest()

    def _backup_name(self, file_path: str, version: int) -> str:
        digest = hashlib.sha256(file_path.encode()).hexdigest()[:16]
        return f"{digest}@v{version}"

    def track_edit(self, path: str) -> None:
        with self._lock:
            abs_path = str(Path(path).expanduser().resolve())
            new_version = self._tracked.get(abs_path, 0) + 1
            source = Path(abs_path)
            existed = source.exists() or source.is_symlink()
            if existed:
                data = source.read_bytes()
                self._atomic_write(
                    self._session_dir / self._backup_name(abs_path, new_version),
                    data,
                )
            self._tracked[abs_path] = new_version
            self._version_existed.setdefault(abs_path, {})[new_version] = existed
            self._persist_manifest()

    def make_snapshot(self, msg_index: int, user_text: str) -> None:
        with self._lock:
            now = time.time()
            backups: dict[str, Backup] = {}
            for path, version in self._tracked.items():
                backup_path = self._session_dir / self._backup_name(path, version)
                existed = self._version_existed.get(path, {}).get(
                    version, backup_path.exists()
                )
                backups[path] = Backup(
                    backup_path=str(backup_path),
                    version=version,
                    timestamp=now,
                    existed=existed,
                )
            self._snapshots.append(
                Snapshot(
                    message_index=msg_index,
                    user_text=user_text,
                    backups=backups,
                    timestamp=now,
                )
            )
            if len(self._snapshots) > MAX_SNAPSHOTS:
                self._snapshots = self._snapshots[-MAX_SNAPSHOTS:]
            self._cleanup_backups()
            self._persist_manifest()

    def get_snapshots(self) -> list[Snapshot]:
        with self._lock:
            return list(self._snapshots)

    def has_snapshots(self) -> bool:
        with self._lock:
            return bool(self._snapshots)

    def rewind(self, snapshot_index: int) -> list[str]:
        with self._lock:
            if snapshot_index < 0 or snapshot_index >= len(self._snapshots):
                return []
            target = self._snapshots[snapshot_index]
            changed: list[str] = []

            for file_path in list(self._tracked):
                backup = target.backups.get(file_path)
                if backup is None:
                    versions = self._version_existed.get(file_path, {})
                    if not versions:
                        continue
                    first_version = min(versions)
                    backup = Backup(
                        backup_path=str(
                            self._session_dir
                            / self._backup_name(file_path, first_version)
                        ),
                        version=first_version,
                        timestamp=target.timestamp,
                        existed=versions[first_version],
                    )
                if self._restore(file_path, backup):
                    changed.append(file_path)

            self._snapshots = self._snapshots[: snapshot_index + 1]
            target_paths = set(target.backups)
            for file_path in list(self._tracked):
                if file_path not in target_paths:
                    self._tracked.pop(file_path, None)
                    self._version_existed.pop(file_path, None)
                    continue
                version = target.backups[file_path].version
                self._tracked[file_path] = version
                versions = self._version_existed.get(file_path, {})
                self._version_existed[file_path] = {
                    item_version: existed
                    for item_version, existed in versions.items()
                    if item_version <= version
                }
            self._cleanup_backups()
            self._persist_manifest()
            return changed

    def _restore(self, file_path: str, backup: Backup) -> bool:
        target = Path(file_path)
        if not backup.existed:
            if target.exists() or target.is_symlink():
                target.unlink()
                return True
            return False
        source = Path(backup.backup_path)
        try:
            data = source.read_bytes()
        except OSError:
            return False
        try:
            current = target.read_bytes()
        except OSError:
            current = None
        if current == data:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, data)
        return True

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)

    def _cleanup_backups(self) -> None:
        keep: set[str] = set()
        for snapshot in self._snapshots:
            keep.update(
                Path(backup.backup_path).name
                for backup in snapshot.backups.values()
                if backup.existed
            )
        for path, version in self._tracked.items():
            if self._version_existed.get(path, {}).get(version, False):
                keep.add(self._backup_name(path, version))
        for entry in self._session_dir.iterdir():
            if entry.name == self._manifest_path.name or entry.suffix == ".tmp":
                continue
            if entry.is_file() and entry.name not in keep:
                try:
                    entry.unlink()
                except OSError:
                    pass

    def _persist_manifest(self) -> None:
        snapshots = []
        for snapshot in self._snapshots:
            row = asdict(snapshot)
            for backup in row["backups"].values():
                backup["backup_path"] = Path(backup["backup_path"]).name
            snapshots.append(row)
        payload = {
            "version": MANIFEST_VERSION,
            "tracked": self._tracked,
            "version_existed": {
                path: {str(version): existed for version, existed in versions.items()}
                for path, versions in self._version_existed.items()
            },
            "snapshots": snapshots,
        }
        temp = self._manifest_path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, self._manifest_path)

    def _load_manifest(self) -> None:
        if not self._manifest_path.exists():
            return
        try:
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("version") != MANIFEST_VERSION:
            return
        tracked = payload.get("tracked", {})
        if isinstance(tracked, dict):
            self._tracked = {str(path): int(version) for path, version in tracked.items()}
        raw_existed = payload.get("version_existed", {})
        if isinstance(raw_existed, dict):
            self._version_existed = {
                str(path): {
                    int(version): bool(existed)
                    for version, existed in versions.items()
                }
                for path, versions in raw_existed.items()
                if isinstance(versions, dict)
            }
        for row in payload.get("snapshots", []):
            try:
                backups = {
                    str(path): Backup(
                        backup_path=str(
                            self._session_dir / Path(str(data["backup_path"])).name
                        ),
                        version=int(data["version"]),
                        timestamp=float(data.get("timestamp", 0.0)),
                        existed=bool(data.get("existed", True)),
                    )
                    for path, data in row.get("backups", {}).items()
                }
                self._snapshots.append(
                    Snapshot(
                        message_index=int(row.get("message_index", 0)),
                        user_text=str(row.get("user_text", "")),
                        backups=backups,
                        timestamp=float(row.get("timestamp", 0.0)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

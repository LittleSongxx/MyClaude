from __future__ import annotations

import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}


@contextmanager
def locked_path(path: Path) -> Iterator[None]:
    """Serialize in-process reads/checks/writes for a resolved file path."""
    key = str(path.resolve())
    with _locks_guard:
        lock = _path_locks.setdefault(key, threading.RLock())
    with lock:
        yield


def atomic_write_text(path: Path, content: str) -> None:
    """Durably replace ``path`` without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
        if os.name == "posix":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

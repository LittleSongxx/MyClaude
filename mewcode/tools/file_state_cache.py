# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

import threading
from pathlib import Path


class FileStateCache:
    """Tracks which files have been read, enforcing read-before-edit.

    Stores { absolute_path: (content, mtime_ns) } after each ReadFile call.
    EditFile and WriteFile check the cache before proceeding:
      - Gate 1: file must have been read (present in cache).
      - Gate 2: file must not have been modified since the read (mtime_ns matches).
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, int]] = {}
        self._lock = threading.RLock()

    def record(self, path: str, content: str, mtime_ns: int) -> None:
        """Record a file's content and mtime after a successful read."""
        with self._lock:
            self._cache[path] = (content, mtime_ns)

    def check(self, path: str) -> tuple[bool, str]:
        """Check whether the file is safe to edit/write.

        Returns (ok, error_message). If ok is True, error_message is empty.
        """
        with self._lock:
            entry = self._cache.get(path)
        if entry is None:
            return False, "Error: file has not been read yet. Read it first before editing."

        cached_content, cached_mtime_ns = entry
        try:
            current = Path(path)
            current_mtime_ns = current.stat().st_mtime_ns
        except OSError:
            # File may have been deleted; allow the write to proceed
            # (WriteFile will create it, EditFile will fail on its own).
            return True, ""

        if current_mtime_ns != cached_mtime_ns:
            return False, "Error: file has been modified since last read. Read it again before editing."

        try:
            if current.read_text(encoding="utf-8") != cached_content:
                return False, "Error: file content has changed since last read. Read it again before editing."
        except OSError:
            return False, "Error: file could not be verified before editing."

        return True, ""

    def update(self, path: str) -> None:
        """Update the cache entry after a successful edit/write."""
        try:
            p = Path(path)
            content = p.read_text(encoding="utf-8")
            mtime_ns = p.stat().st_mtime_ns
            with self._lock:
                self._cache[path] = (content, mtime_ns)
        except OSError:
            # If we can't read it back, just remove the stale entry.
            with self._lock:
                self._cache.pop(path, None)

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._cache.pop(path, None)

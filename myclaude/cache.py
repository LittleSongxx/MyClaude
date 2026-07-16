from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class _CacheEntry:
    content: str
    mtime_ns: int | None = None
    size: int | None = None


class FileCache:
    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(
        self,
        path: str,
        *,
        mtime_ns: int | None = None,
        size: int | None = None,
    ) -> str | None:
        with self._lock:
            entry = self._store.get(path)
            if entry is None:
                return None
            if mtime_ns is not None:
                if entry.mtime_ns is None or entry.mtime_ns != mtime_ns:
                    self._store.pop(path, None)
                    return None
            if size is not None:
                if entry.size is None or entry.size != size:
                    self._store.pop(path, None)
                    return None
            return entry.content


    def put(
        self,
        path: str,
        content: str,
        *,
        mtime_ns: int | None = None,
        size: int | None = None,
    ) -> None:
        with self._lock:
            self._store[path] = _CacheEntry(content, mtime_ns, size)


    def invalidate(self, path: str) -> None:
        with self._lock:
            self._store.pop(path, None)


    def clear(self) -> None:
        with self._lock:
            self._store.clear()


    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def __bool__(self) -> bool:
        """A cache instance is usable even while it is empty.

        ``ReadFile`` historically used ``if cache`` checks.  Because ``__len__``
        returned zero for a new cache, those checks disabled the cache forever.
        Keeping cache availability separate from cache size prevents that class
        of bug at every call site.
        """
        return True

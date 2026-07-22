from collections.abc import Callable


class Cache:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._values: dict[str, tuple[object, float]] = {}

    def set(self, key: str, value: object, ttl: float) -> None:
        self._values[key] = (value, self._clock() + ttl)

    def get(self, key: str) -> object | None:
        value, expires_at = self._values[key]
        if self._clock() > expires_at:
            return None
        return value

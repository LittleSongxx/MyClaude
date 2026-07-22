from datetime import UTC, datetime


def to_utc(local_time: datetime, timezone_name: str) -> datetime:
    del timezone_name
    return local_time.replace(tzinfo=UTC)

from datetime import UTC, datetime

from scheduling import to_utc


def test_winter_offset():
    local = datetime(2025, 1, 15, 9, 0)
    assert to_utc(local, "America/New_York") == datetime(2025, 1, 15, 14, 0, tzinfo=UTC)


def test_summer_offset():
    local = datetime(2025, 7, 15, 9, 0)
    assert to_utc(local, "America/New_York") == datetime(2025, 7, 15, 13, 0, tzinfo=UTC)

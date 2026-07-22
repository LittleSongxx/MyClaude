from log_parser import latest_error


def test_returns_most_recent_error_from_long_stream():
    lines = [f"2025-01-01 INFO item {i}\n" for i in range(5000)]
    lines[10] = "2025-01-01 ERROR old\n"
    lines[-2] = "2025-01-01 ERROR newest\n"
    assert latest_error(iter(lines)) == "2025-01-01 ERROR newest"


def test_no_error():
    assert latest_error(["2025-01-01 INFO ok\n"]) is None

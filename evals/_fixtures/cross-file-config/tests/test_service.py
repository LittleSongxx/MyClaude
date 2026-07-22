from app.service import Service


def test_configured_timeout_is_used():
    assert Service({"timeout_seconds": 5}).timeout == 5


def test_default_timeout_is_used():
    assert Service({}).timeout == 30

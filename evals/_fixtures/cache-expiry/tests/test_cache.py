from cache import Cache


def test_entry_expires_at_deadline():
    now = [10.0]
    cache = Cache(lambda: now[0])
    cache.set("token", "value", ttl=5)
    now[0] = 15.0
    assert cache.get("token") is None


def test_entry_visible_before_deadline():
    now = [10.0]
    cache = Cache(lambda: now[0])
    cache.set("token", "value", ttl=5)
    now[0] = 14.999
    assert cache.get("token") == "value"

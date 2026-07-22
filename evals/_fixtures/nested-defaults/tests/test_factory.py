from factory import DEFAULT_SETTINGS, make_settings


def test_nested_values_are_independent():
    first = make_settings()
    second = make_settings()
    first["headers"]["X-Test"] = "one"
    assert second["headers"] == {"User-Agent": "fixture"}
    assert DEFAULT_SETTINGS["headers"] == {"User-Agent": "fixture"}

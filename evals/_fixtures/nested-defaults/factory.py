DEFAULT_SETTINGS = {
    "timeout": 30,
    "headers": {"User-Agent": "fixture"},
}


def make_settings() -> dict[str, object]:
    return DEFAULT_SETTINGS.copy()

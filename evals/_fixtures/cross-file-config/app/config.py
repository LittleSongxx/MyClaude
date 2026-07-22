DEFAULTS = {"timeout_seconds": 30}


def request_timeout(settings: dict[str, int]) -> int:
    return settings.get("timeout", DEFAULTS["timeout_seconds"])

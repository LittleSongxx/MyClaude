from collections.abc import Iterable


def latest_error(lines: Iterable[str]) -> str | None:
    for line in lines:
        if " ERROR " in line:
            return line.rstrip("\n")
    return None

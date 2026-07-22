from pathlib import Path


def resolve_under(root: Path, user_path: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / user_path).resolve()
    if not str(candidate).startswith(str(resolved_root)):
        raise ValueError("path escapes storage root")
    return candidate

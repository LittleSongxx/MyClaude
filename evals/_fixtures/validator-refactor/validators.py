def normalize_signup_email(value: str) -> str:
    normalized = value.strip().lower()
    if "@" not in normalized:
        raise ValueError("invalid email")
    return normalized


def normalize_recovery_email(value: str) -> str:
    normalized = value.strip().lower()
    if "@" not in normalized:
        raise ValueError("invalid email")
    return normalized

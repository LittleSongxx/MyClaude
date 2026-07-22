def compact_payload(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if value}

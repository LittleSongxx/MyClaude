from serializer import compact_payload


def test_preserves_falsey_values():
    payload = {"enabled": False, "retries": 0, "label": "", "note": None}
    assert compact_payload(payload) == {"enabled": False, "retries": 0, "label": ""}

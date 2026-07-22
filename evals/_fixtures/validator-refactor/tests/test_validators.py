import pytest

from validators import normalize_email, normalize_recovery_email, normalize_signup_email


def test_public_helper_and_callers_share_behavior():
    for function in (normalize_email, normalize_signup_email, normalize_recovery_email):
        assert function("  Ada@Example.COM ") == "ada@example.com"


def test_invalid_email_rejected():
    for function in (normalize_email, normalize_signup_email, normalize_recovery_email):
        with pytest.raises(ValueError):
            function("invalid")

from app.models import User


def test_user_label():
    assert User("  ada lovelace ").label == "Ada Lovelace"

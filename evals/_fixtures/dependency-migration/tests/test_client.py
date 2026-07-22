from client import upload
from library import Transport


def test_upload_uses_timeout_keyword():
    assert upload(Transport(), b"data") == (b"data", 30.0)

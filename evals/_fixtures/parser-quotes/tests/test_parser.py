from parser import parse_record


def test_quoted_comma():
    assert parse_record('42,"Chen, Li",active') == ["42", "Chen, Li", "active"]


def test_plain_fields():
    assert parse_record("1,Ada,active\n") == ["1", "Ada", "active"]

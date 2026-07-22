from cli import main


def test_invalid_count_returns_usage_error(capsys):
    assert main(["--count", "nope"]) == 2
    assert "invalid" in capsys.readouterr().out


def test_valid_count_succeeds(capsys):
    assert main(["--count", "3"]) == 0
    assert capsys.readouterr().out.strip() == "xxx"

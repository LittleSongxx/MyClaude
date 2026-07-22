from pathlib import Path

import pytest

from storage import resolve_under


def test_nested_path_is_allowed(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    assert resolve_under(root, "users/a.txt") == root / "users" / "a.txt"


def test_sibling_prefix_is_rejected(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    sibling = tmp_path / "database"
    sibling.mkdir()
    with pytest.raises(ValueError):
        resolve_under(root, "../database/secret.txt")

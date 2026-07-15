from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from myclaude.config import load_config
from myclaude.trust import WorkspaceTrustManager, resolve_workspace_root


def test_workspace_root_uses_repository_marker(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    assert resolve_workspace_root(nested) == root.resolve()


def test_trust_round_trip_is_canonical_and_versioned(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "repo"
    child = root / "child"
    child.mkdir(parents=True)
    (root / ".git").mkdir()
    manager = WorkspaceTrustManager(home)

    assert not manager.is_trusted(child)
    assert manager.trust(child) == root.resolve()
    assert manager.is_trusted(root)
    payload = json.loads(manager.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["trusted"] == [str(root.resolve())]
    assert stat.S_IMODE(manager.path.stat().st_mode) == 0o600

    manager.revoke(root)
    assert not manager.is_trusted(child)

    manager.path.write_text(
        json.dumps({"schema_version": True, "trusted": [str(root.resolve())]}),
        encoding="utf-8",
    )
    assert not manager.is_trusted(root)


def test_untrusted_config_loading_ignores_project_layer(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".myclaude").mkdir(parents=True)
    (project / ".myclaude").mkdir(parents=True)
    (home / ".myclaude" / "config.yaml").write_text(
        """
providers:
  - name: user
    protocol: anthropic
    base_url: https://example.invalid
    model: claude-user
""",
        encoding="utf-8",
    )
    (project / ".myclaude" / "config.yaml").write_text(
        """
providers:
  - name: project
    protocol: anthropic
    base_url: https://example.invalid
    model: claude-project
hooks:
  - event: startup
    action:
      type: command
      command: echo unsafe
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    config = load_config(include_project=False, cwd=project)

    assert config.providers[0].name == "user"
    assert config.raw_hooks == []


def test_headless_cli_fails_closed_for_untrusted_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from myclaude.__main__ import main

    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["myclaude", "-p", "inspect this project"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "workspace is not trusted" in capsys.readouterr().err
    assert not (project / ".myclaude").exists()

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

log = logging.getLogger(__name__)

VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]*$")
VALID_MODES = {"inline", "fork"}
VALID_CONTEXTS = {"full", "recent", "none", "fork"}
MAX_DYNAMIC_CONTEXT_CHARS = 20_000


class SkillParseError(Exception):
    pass


@dataclass
class SkillDef:
    name: str
    description: str
    prompt_body: str = ""
    mode: Literal["inline", "fork"] = "inline"
    model: str | None = None
    context: Literal["full", "recent", "none"] = "full"
    source_path: Path | None = None
    is_directory: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    disable_model_invocation: bool = False
    user_invocable: bool = True
    argument_hint: str = ""
    agent: str = ""


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    stripped = raw.lstrip()
    if not stripped.startswith("---"):
        raise SkillParseError("Missing YAML frontmatter (must start with ---)")

    end = stripped.find("---", 3)
    if end == -1:
        raise SkillParseError("Unclosed YAML frontmatter (missing closing ---)")

    yaml_block = stripped[3:end]
    body = stripped[end + 3:].lstrip("\n")

    try:
        meta = yaml.safe_load(yaml_block)
    except yaml.YAMLError as e:
        raise SkillParseError(f"Invalid YAML in frontmatter: {e}") from e

    if not isinstance(meta, dict):
        raise SkillParseError("Frontmatter must be a YAML mapping")

    return meta, body


def _validate_meta(meta: dict, source: str = "") -> None:
    ctx = f" in {source}" if source else ""

    if "name" not in meta:
        raise SkillParseError(f"Missing required field 'name'{ctx}")
    if "description" not in meta:
        raise SkillParseError(f"Missing required field 'description'{ctx}")

    name = meta["name"]
    if not isinstance(name, str) or not VALID_NAME_RE.match(name):
        raise SkillParseError(
            f"Invalid skill name '{name}'{ctx}: "
            "must be lowercase letters, digits, and hyphens, starting with a letter"
        )
    description = meta["description"]
    if not isinstance(description, str) or not description.strip():
        raise SkillParseError(f"Invalid skill description{ctx}: must be non-empty text")

    mode = meta.get("mode", "inline")
    if mode not in VALID_MODES:
        raise SkillParseError(f"Invalid mode '{mode}'{ctx}: must be one of {VALID_MODES}")

    context = meta.get("context", "full")
    if context not in VALID_CONTEXTS:
        raise SkillParseError(f"Invalid context '{context}'{ctx}: must be one of {VALID_CONTEXTS}")


def parse_skill_file(path: Path) -> SkillDef:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SkillParseError(f"Cannot read skill file {path}: {e}") from e

    meta, body = parse_frontmatter(raw)
    _validate_meta(meta, str(path))

    context = str(meta.get("context", "full"))
    mode = str(meta.get("mode", "inline"))
    if context == "fork":
        mode = "fork"
        context = "none"
    return SkillDef(
        name=meta["name"],
        description=meta["description"],
        prompt_body=body,
        mode=mode,
        model=meta.get("model"),
        context=context,
        source_path=path,
        is_directory=False,
        allowed_tools=_tool_list(meta.get("allowed-tools", meta.get("allowedTools", []))),
        disallowed_tools=_tool_list(
            meta.get("disallowed-tools", meta.get("disallowedTools", []))
        ),
        disable_model_invocation=bool(meta.get("disable-model-invocation", False)),
        user_invocable=bool(meta.get("user-invocable", True)),
        argument_hint=str(meta.get("argument-hint", "")),
        agent=str(meta.get("agent", "")),
    )


def _tool_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def substitute_arguments(prompt_body: str, args: str) -> str:
    """将 $ARGUMENTS 占位符替换为用户请求。

    若 prompt_body 中不含 $ARGUMENTS 占位符且 args 非空，
    则将用户请求追加到末尾（append fallback）。
    """
    try:
        positional = shlex.split(args)
    except ValueError:
        positional = args.split()
    had_placeholder = "$ARGUMENTS" in prompt_body or bool(
        re.search(r"\$(?:ARGUMENTS\[)?\d+\]?", prompt_body)
    )

    def indexed(match: re.Match[str]) -> str:
        raw_index = match.group(1) or match.group(2)
        index = int(raw_index)
        return positional[index] if index < len(positional) else ""

    result = re.sub(r"\$ARGUMENTS\[(\d+)\]|\$(\d+)", indexed, prompt_body)
    result = result.replace("$ARGUMENTS", args)
    if had_placeholder:
        return result
    # 无占位符时的 append fallback
    if args.strip():
        return prompt_body + "\n\n## User Request\n\n" + args
    return prompt_body


_DYNAMIC_CONTEXT_RE = re.compile(r"(?m)^\s*!`([^`\n]+)`\s*$")


def dynamic_context_commands(prompt_body: str) -> list[str]:
    return [match.group(1) for match in _DYNAMIC_CONTEXT_RE.finditer(prompt_body)]


def expand_dynamic_context(
    prompt_body: str,
    work_dir: str,
    *,
    timeout: float = 10.0,
) -> str:
    """Expand Agent Skills ``!`command``` lines with bounded command output."""

    def run(match: re.Match[str]) -> str:
        command = match.group(1)
        try:
            completed = subprocess.run(
                command,
                cwd=work_dir,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"[dynamic context failed: {e}]"
        output = (completed.stdout + completed.stderr).strip()
        if len(output) > MAX_DYNAMIC_CONTEXT_CHARS:
            output = output[:MAX_DYNAMIC_CONTEXT_CHARS] + "\n... (truncated)"
        if completed.returncode:
            return f"[command exited {completed.returncode}]\n{output}".rstrip()
        return output

    return _DYNAMIC_CONTEXT_RE.sub(run, prompt_body)

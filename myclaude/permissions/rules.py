from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml

from myclaude.tools.file_io import atomic_write_text, locked_path

Effect = Literal["allow", "deny", "ask"]
MatchMode = Literal["glob", "literal"]

_RULE_RE = re.compile(r"^(\w+)\((.+)\)$", re.DOTALL)

_CONTENT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "DeleteFile": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}

_PATH_FIELDS: dict[str, str] = {
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "DeleteFile": "file_path",
    "Glob": "path",
    "Grep": "path",
}


@dataclass(frozen=True)
class Rule:
    tool_name: str
    pattern: str
    effect: Effect
    match: MatchMode = "glob"


    def matches(self, tool_name: str, content: str) -> bool:
        if self.tool_name != tool_name:
            return False
        if self.match == "literal":
            return content == self.pattern
        return fnmatch(content, self.pattern)


def parse_rule(raw: str, effect: Effect) -> Rule:
    m = _RULE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无效的规则语法: {raw}")
    return Rule(tool_name=m.group(1), pattern=m.group(2), effect=effect)


def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    field = _CONTENT_FIELDS.get(tool_name)
    if field is None:
        return ""
    return str(arguments.get(field, ""))


def extract_path(tool_name: str, arguments: dict[str, Any]) -> str:
    """Return the filesystem scope used for sandbox checks.

    Permission rules intentionally keep matching Grep/Glob by their search
    pattern, but the path sandbox must validate their ``path`` argument.  A
    single extractor for both concerns previously allowed searches outside the
    project whenever the regex/glob itself looked like an in-project path.
    """
    field = _PATH_FIELDS.get(tool_name)
    if field is None:
        return ""
    default = "." if tool_name in {"Glob", "Grep"} else ""
    return str(arguments.get(field, default))


def _load_rules_file(path: Path) -> list[Rule]:
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rule_str = entry.get("rule", "")
        effect = entry.get("effect", "")
        match = entry.get("match", "glob")
        if effect not in ("allow", "deny", "ask"):
            continue
        if match not in ("glob", "literal"):
            continue
        try:
            parsed = parse_rule(rule_str, effect)
            rules.append(
                Rule(
                    tool_name=parsed.tool_name,
                    pattern=parsed.pattern,
                    effect=parsed.effect,
                    match=match,
                )
            )
        except ValueError:
            continue
    return rules


class RuleEngine:


    def __init__(
        self,
        user_rules_path: Path | None = None,
        project_rules_path: Path | None = None,
        local_rules_path: Path | None = None,
    ) -> None:
        self._user_path = user_rules_path
        self._project_path = project_rules_path
        self._local_path = local_rules_path

    def _load_tiers(self) -> list[list[Rule]]:
        tiers: list[list[Rule]] = []
        for p in (self._user_path, self._project_path, self._local_path):
            tiers.append(_load_rules_file(p) if p else [])
        return tiers


    def evaluate(self, tool_name: str, content: str) -> Effect | None:
        for rules in self._load_tiers():
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule.effect
        return None


    def append_local_rule(self, rule: Rule) -> None:
        if self._local_path is None:
            return
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        # 权限规则是安全配置，read-modify-write 必须串行化并原子落盘：
        # 非原子 write_text 在并发 ALLOW_ALWAYS 下会丢失更新或写出半截文件。
        with locked_path(self._local_path):
            existing = _load_rules_file(self._local_path)
            existing.append(rule)
            entries = [
                {
                    "rule": f"{r.tool_name}({r.pattern})",
                    "effect": r.effect,
                    "match": r.match,
                }
                for r in existing
            ]
            atomic_write_text(
                self._local_path, yaml.dump(entries, allow_unicode=True)
            )

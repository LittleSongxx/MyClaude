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

_EFFECT_PRIORITY: dict[Effect, int] = {"allow": 1, "ask": 2, "deny": 3}
_SHELL_SEPARATORS = ("&&", "||", "|&", ";", "|", "&", "\n", "\r")

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


def _split_bash_command(command: str) -> tuple[list[str], bool]:
    """Split top-level shell commands without treating quoted operators as syntax.

    The boolean reports whether the expression is simple enough for an allow
    rule to authorize. Command/process substitution and grouping constructs
    are intentionally treated as ambiguous; deny and ask rules are still
    checked, but an allow falls back to the normal permission prompt.
    """
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    unambiguous = True
    index = 0

    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            index += 1
            continue

        if char == "`" or command.startswith("$(", index):
            unambiguous = False
        if char in {"(", ")", "{", "}"}:
            unambiguous = False

        separator = next(
            (item for item in _SHELL_SEPARATORS if command.startswith(item, index)),
            "",
        )
        if separator:
            part = "".join(current).strip()
            if part:
                segments.append(part)
            current = []
            index += len(separator)
            continue
        current.append(char)
        index += 1

    part = "".join(current).strip()
    if part:
        segments.append(part)
    if quote or escaped or not segments:
        unambiguous = False
    return segments, unambiguous


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


    def _all_rules(self) -> list[Rule]:
        # Reverse each source so a recently appended rule is inspected first,
        # while effect priority remains global across every settings scope.
        return [rule for tier in self._load_tiers() for rule in reversed(tier)]

    @staticmethod
    def _resolve_matching(
        rules: list[Rule], tool_name: str, content: str
    ) -> Effect | None:
        effects = [
            rule.effect for rule in rules if rule.matches(tool_name, content)
        ]
        if not effects:
            return None
        return max(effects, key=_EFFECT_PRIORITY.__getitem__)


    def evaluate(self, tool_name: str, content: str) -> Effect | None:
        rules = self._all_rules()
        whole_result = self._resolve_matching(rules, tool_name, content)
        if tool_name != "Bash":
            return whole_result

        segments, unambiguous = _split_bash_command(content)
        segment_results = [
            self._resolve_matching(rules, tool_name, segment) for segment in segments
        ]

        # A deny or ask on either the complete expression or any executable
        # component always wins. This also preserves conservative broad rules.
        observed = [whole_result, *segment_results]
        if "deny" in observed:
            return "deny"
        if "ask" in observed:
            return "ask"

        # A compound expression is allowed only when every command is covered.
        # Ambiguous shell syntax must go through the regular permission flow.
        if unambiguous and segment_results and all(
            result == "allow" for result in segment_results
        ):
            return "allow"
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

# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

import re
import shlex

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/\s*$"), "递归强制删除根目录"),
    (re.compile(r"mkfs\."), "格式化磁盘"),
    (re.compile(r"dd\s+if=.*of=/dev/"), "直接写磁盘设备"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "递归修改根目录权限"),
    (re.compile(r":\(\)\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"curl\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r"wget\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r">\s*/dev/sd"), "覆盖磁盘设备"),
]


_SAFE_HOST_INFO_COMMANDS = frozenset({
    "pwd", "whoami", "hostname", "uname", "date", "cal", "uptime",
    "df", "free", "true", "false",
})

_SAFE_LOOKUP_COMMANDS = frozenset({"which", "whereis"})

_SAFE_GIT_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "rev-parse", "ls-files", "blame",
})

_SAFE_EXACT_COMMANDS = frozenset({
    "git stash list",
    "go version", "go env",
    "node -v", "node --version", "npm -v", "npm --version",
    "python --version", "python3 --version", "pip list", "pip3 list",
    "cargo --version", "rustc --version", "java -version", "java --version",
})

def is_safe_command(command: str) -> bool:
    trimmed = command.strip()
    if not trimmed:
        return False
    # Be deliberately conservative: ambiguity means asking the user, not
    # silently granting command execution.  Newlines and all shell composition
    # operators are excluded even when they occur after a read-only prefix.
    for ch in (
        "|", ";", "&&", "||", ">", "<", "$", "`", "\n", "\r",
        "*", "?", "[", "]", "{", "}", "~",
    ):
        if ch in trimmed:
            return False
    try:
        tokens = shlex.split(trimmed)
    except ValueError:
        return False
    if not tokens:
        return False

    normalized = " ".join(tokens)
    if normalized in _SAFE_EXACT_COMMANDS:
        return True

    command_name = tokens[0].rsplit("/", maxsplit=1)[-1]
    if command_name == "git":
        unsafe_option = any(
            token in {"--no-index", "--ext-diff", "--textconv", "--exec-path"}
            or token.startswith(("--output=", "--git-dir=", "--work-tree="))
            or token == "--output"
            for token in tokens[2:]
        )
        return (
            len(tokens) >= 2
            and tokens[1] in _SAFE_GIT_SUBCOMMANDS
            and not unsafe_option
        )

    if command_name in {"ls", "dir"}:
        # Directory listing is automatic only for the agent's working
        # directory.  Explicit paths should go through Glob/ReadFile so the
        # path sandbox can enforce project scope.
        return all(token.startswith("-") for token in tokens[1:])

    if command_name in _SAFE_LOOKUP_COMMANDS:
        return all(re.fullmatch(r"[A-Za-z0-9_.+-]+", token) for token in tokens[1:])

    if command_name in _SAFE_HOST_INFO_COMMANDS:
        return all(token.startswith(("-", "+")) for token in tokens[1:])

    return False


class DangerousCommandDetector:


    def __init__(self, extra_patterns: list[tuple[str, str]] | None = None) -> None:
        self._patterns = list(_DANGEROUS_PATTERNS)
        if extra_patterns:
            for regex_str, reason in extra_patterns:
                self._patterns.append((re.compile(regex_str), reason))


    def detect(self, command: str) -> tuple[bool, str]:
        for pattern, reason in self._patterns:
            if pattern.search(command):
                return True, reason
        return False, ""

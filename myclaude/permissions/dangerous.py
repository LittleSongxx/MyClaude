# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

import re
import shlex
import posixpath

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"mkfs\."), "格式化磁盘"),
    (re.compile(r"dd\s+if=.*of=/dev/"), "直接写磁盘设备"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "递归修改根目录权限"),
    (re.compile(r":\(\)\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"curl\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r"wget\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r">\s*/dev/sd"), "覆盖磁盘设备"),
]

_SHELL_SEPARATORS = {";", "&&", "||", "|", "&"}
_COMMAND_WRAPPERS = {"command", "exec", "nohup"}
_SUDO_OPTIONS_WITH_VALUE = {
    "-C", "--chdir",
    "-g", "--group",
    "-h", "--host",
    "-p", "--prompt",
    "-R", "--chroot",
    "-r", "--role",
    "-t", "--type",
    "-u", "--user",
}
_ENV_OPTIONS_WITH_VALUE = {
    "-C", "--chdir",
    "-u", "--unset",
}


def _consume_wrapper_options(
    tokens: list[str], options_with_value: set[str]
) -> None:
    while tokens:
        token = tokens[0]
        if token == "--":
            tokens.pop(0)
            return
        if not token.startswith("-") or token == "-":
            return
        tokens.pop(0)
        option = token.split("=", maxsplit=1)[0]
        if option in options_with_value and "=" not in token and tokens:
            tokens.pop(0)


def _shell_segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(
            command.replace("\n", " ; ").replace("\r", " ; "),
            posix=True,
            punctuation_chars=";&|<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _unwrap_command(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    while remaining:
        token = remaining[0]
        if re.fullmatch(r"[A-Za-z_]\w*=.*", token):
            remaining.pop(0)
            continue
        base = token.rsplit("/", maxsplit=1)[-1]
        if base in _COMMAND_WRAPPERS:
            remaining.pop(0)
            if base == "command" and any(
                option in {"-v", "-V"} for option in remaining
            ):
                return []
            _consume_wrapper_options(remaining, {"-a"} if base == "exec" else set())
            continue
        if base == "sudo":
            remaining.pop(0)
            _consume_wrapper_options(remaining, _SUDO_OPTIONS_WITH_VALUE)
            continue
        if base == "env":
            remaining.pop(0)
            _consume_wrapper_options(remaining, _ENV_OPTIONS_WITH_VALUE)
            while remaining and re.fullmatch(
                r"[A-Za-z_]\w*=.*", remaining[0]
            ):
                remaining.pop(0)
            continue
        if base == "nice":
            remaining.pop(0)
            _consume_wrapper_options(remaining, {"-n", "--adjustment"})
            continue
        if base == "timeout":
            remaining.pop(0)
            _consume_wrapper_options(remaining, {"-k", "--kill-after", "-s", "--signal"})
            if remaining:
                remaining.pop(0)  # duration
            continue
        if base == "setsid":
            remaining.pop(0)
            _consume_wrapper_options(remaining, set())
            continue
        break
    return remaining


def _is_root_target(value: str) -> bool:
    normalized = re.sub(r"/+", "/", value.strip())
    collapsed = posixpath.normpath(normalized)
    return (
        normalized in {"/", "/.", "/*"}
        or collapsed == "/"
        or normalized.startswith("/*/")
    )


def _detect_structured_command(
    command: str, *, depth: int = 0
) -> tuple[bool, str]:
    if depth > 4:
        return False, ""
    for raw_segment in _shell_segments(command):
        tokens = _unwrap_command(raw_segment)
        if not tokens:
            continue
        name = tokens[0].rsplit("/", maxsplit=1)[-1]
        args = tokens[1:]

        if name == "busybox" and args:
            name, args = args[0], args[1:]

        if name in {"bash", "dash", "ksh", "sh", "zsh"}:
            for index, arg in enumerate(args):
                if (
                    re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", arg)
                    and index + 1 < len(args)
                ):
                    hit, reason = _detect_structured_command(
                        args[index + 1], depth=depth + 1
                    )
                    if hit:
                        return hit, reason
                    break

        if name == "rm":
            recursive = False
            targets: list[str] = []
            options_done = False
            for arg in args:
                if not options_done and arg == "--":
                    options_done = True
                    continue
                if not options_done and arg.startswith("--"):
                    recursive = recursive or arg in {"--recursive", "--dir"}
                    continue
                if not options_done and arg.startswith("-") and arg != "-":
                    recursive = recursive or "r" in arg[1:] or "R" in arg[1:]
                    continue
                targets.append(arg)
            if recursive and any(_is_root_target(target) for target in targets):
                return True, "递归删除根目录"

        if name in {"chmod", "chown", "chgrp"}:
            recursive = any(
                arg in {"-R", "--recursive"} or (arg.startswith("-") and "R" in arg[1:])
                for arg in args
            )
            if recursive and any(_is_root_target(arg) for arg in args if not arg.startswith("-")):
                return True, "递归修改根目录权限或所有权"

        if name == "find" and args and _is_root_target(args[0]) and "-delete" in args:
            return True, "递归删除根目录内容"

        if name == "dd" and any(
            arg.startswith("of=/dev/") for arg in args
        ):
            return True, "直接写磁盘设备"

        if name in {"shred", "wipefs"} and any(arg.startswith("/dev/") for arg in args):
            return True, "破坏磁盘设备"

    return False, ""


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
        hit, reason = _detect_structured_command(command)
        if hit:
            return hit, reason
        for pattern, reason in self._patterns:
            if pattern.search(command):
                return True, reason
        return False, ""

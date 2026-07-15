# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""MyClaude 的配置校验逻辑。"""

from __future__ import annotations

import math
from pathlib import Path

from myclaude.model_capabilities import lookup_model_capabilities

VALID_PROTOCOLS = {"anthropic", "openai", "openai-compat"}

VALID_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
}

VALID_TEAMMATE_MODES = {"", "in-process"}

DEFAULT_CONTEXT_WINDOW = 200_000

# 内置的"模型名子串 -> context window（最大输入 token 数）"映射表，
# 是 context window 回退链的第 3 层（见 ProviderConfig.get_context_window）。
# 按从最具体到最通用排序，第一个子串命中即生效。值仅为合理起始点，
# 模型更新/重命名后可能过时。如果值不准确，在配置中设置 context_window 覆盖（最高优先级）。
def lookup_model_context_window(model: str) -> int:
    """通过子串匹配（第 3 层），返回内置映射表中该模型对应的
    context window；没有匹配则返回 0。"""
    capabilities = lookup_model_capabilities(model)
    return capabilities.context_window if capabilities is not None else 0


class ConfigError(Exception):
    pass


def validate_providers(raw_providers: list) -> list[dict]:
    """校验 providers 列表，返回清洗后的 provider 字典列表。"""
    if not isinstance(raw_providers, list) or len(raw_providers) == 0:
        raise ConfigError("At least one provider must be configured")

    providers: list[dict] = []
    for i, entry in enumerate(raw_providers):
        if not isinstance(entry, dict):
            raise ConfigError(f"Provider #{i + 1}: must be a mapping")

        missing = [f for f in ("name", "protocol", "base_url", "model") if f not in entry]
        if missing:
            raise ConfigError(f"Provider #{i + 1}: missing fields: {', '.join(missing)}")

        for field_name in ("name", "protocol", "base_url", "model"):
            value = entry[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(
                    f"Provider #{i + 1}: '{field_name}' must be a non-empty string"
                )

        protocol = entry["protocol"]
        if protocol not in VALID_PROTOCOLS:
            raise ConfigError(
                f"Provider #{i + 1}: invalid protocol '{protocol}', "
                f"must be one of: {', '.join(sorted(VALID_PROTOCOLS))}"
            )

        # 默认为 0（"未设置"）而非硬编码的 window 值：0 会让
        # ProviderConfig.get_context_window() 走四层回退链解析
        #（自动拉取 / 映射表 / 默认值）。配置中显式指定的值仍须为正整数，
        # 且作为最高优先级覆盖。
        context_window = entry.get("context_window", 0)
        if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window < 0:
            raise ConfigError(
                f"Provider #{i + 1}: context_window must be a positive integer"
            )

        thinking = entry.get("thinking", False)
        if not isinstance(thinking, bool):
            raise ConfigError(f"Provider #{i + 1}: thinking must be a boolean")

        max_output_tokens = entry.get("max_output_tokens", 0)
        if (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens < 0
        ):
            raise ConfigError(
                f"Provider #{i + 1}: max_output_tokens must be a non-negative integer"
            )

        api_key = entry.get("api_key", "")
        if not isinstance(api_key, str):
            raise ConfigError(f"Provider #{i + 1}: api_key must be a string")

        costs: dict[str, float] = {}
        for field_name in ("input_cost_per_million", "output_cost_per_million"):
            value = entry.get(field_name, 0.0)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
                or not math.isfinite(float(value))
            ):
                raise ConfigError(
                    f"Provider #{i + 1}: {field_name} must be a non-negative number"
                )
            costs[field_name] = float(value)

        providers.append(
            {
                "name": entry["name"],
                "protocol": protocol,
                "base_url": entry["base_url"],
                "model": entry["model"],
                "api_key": api_key,
                "thinking": thinking,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
                **costs,
            }
        )

    return providers


def validate_permission_mode(mode: str) -> str:
    """校验 permission_mode 取值。"""
    if mode not in VALID_PERMISSION_MODES:
        raise ConfigError(
            f"Invalid permission_mode '{mode}', "
            f"must be one of: {', '.join(sorted(VALID_PERMISSION_MODES))}"
        )
    return mode


def validate_mcp_servers(raw_mcp: list | None) -> list[dict]:
    """校验 mcp_servers 配置段，返回清洗后的 server 配置字典列表。"""
    if raw_mcp is None:
        return []

    if not isinstance(raw_mcp, list):
        raise ConfigError("'mcp_servers' must be a list of server configs")

    servers: list[dict] = []
    for i, entry in enumerate(raw_mcp):
        if not isinstance(entry, dict):
            raise ConfigError(f"MCP server #{i + 1}: must be a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"MCP server #{i + 1}: 'name' must be a non-empty string")
        has_command = "command" in entry
        has_url = "url" in entry
        if has_command and has_url:
            raise ConfigError(
                f"MCP server '{name}': cannot have both 'command' and 'url'"
            )
        if not has_command and not has_url:
            raise ConfigError(
                f"MCP server '{name}': must have either 'command' or 'url'"
            )
        command = entry.get("command")
        url = entry.get("url")
        if command is not None and (
            not isinstance(command, str) or not command.strip()
        ):
            raise ConfigError(f"MCP server '{name}': command must be a non-empty string")
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise ConfigError(f"MCP server '{name}': url must be a non-empty string")
        args = entry.get("args", [])
        headers = entry.get("headers", {})
        env = entry.get("env", {})
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ConfigError(f"MCP server '{name}': args must be a list of strings")
        for field_name, value in (("headers", headers), ("env", env)):
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in value.items()
            ):
                raise ConfigError(
                    f"MCP server '{name}': {field_name} must map strings to strings"
                )
        servers.append(
            {
                "name": name,
                "command": command,
                "args": args,
                "url": url,
                "headers": headers,
                "env": env,
            }
        )

    return servers


def validate_hooks(raw_hooks: list | None) -> list:
    """校验 hooks 配置段。"""
    if raw_hooks is None:
        return []
    if not isinstance(raw_hooks, list):
        raise ConfigError("'hooks' must be a list of hook definitions")
    return raw_hooks


def validate_bool_field(value: object, field_name: str) -> bool:
    """校验一个布尔类型的配置字段。"""
    if not isinstance(value, bool):
        raise ConfigError(f"'{field_name}' must be a boolean")
    return value


def validate_worktree(raw_wt: dict | None) -> dict:
    """校验 worktree 配置段，返回清洗后的配置字典。"""
    defaults = {
        "symlink_directories": ["node_modules", ".venv", "vendor"],
        "stale_cleanup_interval": 3600,
        "stale_cutoff_hours": 24,
    }

    if raw_wt is None:
        return defaults

    if not isinstance(raw_wt, dict):
        raise ConfigError("'worktree' must be a mapping")

    sym = raw_wt.get("symlink_directories", defaults["symlink_directories"])
    if not isinstance(sym, list) or not all(isinstance(s, str) for s in sym):
        raise ConfigError("'worktree.symlink_directories' must be a list of strings")
    for directory in sym:
        path = Path(directory)
        if (
            not directory.strip()
            or path.is_absolute()
            or ".." in path.parts
            or path == Path(".")
        ):
            raise ConfigError(
                "'worktree.symlink_directories' entries must be safe relative paths"
            )

    interval = raw_wt.get("stale_cleanup_interval", defaults["stale_cleanup_interval"])
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        raise ConfigError("'worktree.stale_cleanup_interval' must be a positive integer")

    cutoff = raw_wt.get("stale_cutoff_hours", defaults["stale_cutoff_hours"])
    if not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff <= 0:
        raise ConfigError("'worktree.stale_cutoff_hours' must be a positive integer")

    return {
        "symlink_directories": sym,
        "stale_cleanup_interval": interval,
        "stale_cutoff_hours": cutoff,
    }


def validate_teammate_mode(mode: object) -> str:
    """校验 teammate_mode 取值。"""
    if not isinstance(mode, str) or mode not in VALID_TEAMMATE_MODES:
        raise ConfigError(
            f"Invalid teammate_mode '{mode}', "
            f"must be one of: {', '.join(repr(m) for m in sorted(VALID_TEAMMATE_MODES))}"
        )
    return mode


def validate_sandbox(raw_sb: dict | None) -> dict:
    """校验 sandbox 配置段，返回清洗后的配置字典。"""
    defaults = {
        "enabled": False,
        "auto_allow": False,
        "network_enabled": False,
    }

    if raw_sb is None:
        return defaults

    if not isinstance(raw_sb, dict):
        raise ConfigError("'sandbox' must be a mapping")

    result = dict(defaults)
    for key in ("enabled", "auto_allow", "network_enabled"):
        if key in raw_sb:
            val = raw_sb[key]
            if not isinstance(val, bool):
                raise ConfigError(f"'sandbox.{key}' must be a boolean")
            result[key] = val

    return result


def validate_run_limits(raw_limits: dict | None) -> dict:
    defaults: dict[str, int | float] = {
        "max_turns": 0,
        "max_wall_time_seconds": 0.0,
        "max_total_tokens": 0,
        "max_cost_usd": 0.0,
    }
    if raw_limits is None:
        return defaults
    if not isinstance(raw_limits, dict):
        raise ConfigError("'run_limits' must be a mapping")
    unknown = set(raw_limits) - set(defaults)
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ConfigError(f"Unknown run limit field(s): {names}")
    result = dict(defaults)
    for key in ("max_turns", "max_total_tokens"):
        value = raw_limits.get(key, defaults[key])
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"'run_limits.{key}' must be a non-negative integer")
        result[key] = value
    for key in ("max_wall_time_seconds", "max_cost_usd"):
        value = raw_limits.get(key, defaults[key])
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
            or not math.isfinite(float(value))
        ):
            raise ConfigError(f"'run_limits.{key}' must be a non-negative number")
        result[key] = float(value)
    return result


def validate_config_structure(raw: object, *, require_providers: bool = True) -> dict:
    """校验的主入口。校验解析后的原始配置，返回清洗后的字典。

    返回的字典包含以下键：
        providers、permission_mode、mcp_servers、hooks、
        enable_fork、enable_verification_agent、worktree、
        teammate_mode、enable_coordinator_mode、sandbox
    """
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a mapping")
    if require_providers and "providers" not in raw:
        raise ConfigError("Config must contain a 'providers' list")

    return {
        "providers": validate_providers(raw["providers"]) if "providers" in raw else [],
        "permission_mode": validate_permission_mode(raw.get("permission_mode", "default")),
        "mcp_servers": validate_mcp_servers(raw.get("mcp_servers")),
        "hooks": validate_hooks(raw.get("hooks")),
        "enable_fork": validate_bool_field(raw.get("enable_fork", False), "enable_fork"),
        "enable_verification_agent": validate_bool_field(
            raw.get("enable_verification_agent", False), "enable_verification_agent"
        ),
        "worktree": validate_worktree(raw.get("worktree")),
        "teammate_mode": validate_teammate_mode(raw.get("teammate_mode", "")),
        "enable_coordinator_mode": validate_bool_field(
            raw.get("enable_coordinator_mode", False), "enable_coordinator_mode"
        ),
        "sandbox": validate_sandbox(raw.get("sandbox")),
        "run_limits": validate_run_limits(raw.get("run_limits")),
    }

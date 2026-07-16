from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from myclaude.model_capabilities import resolve_model_capabilities
from myclaude.usage import RunLimits

from .validator import (
    ConfigError,
    DEFAULT_CONTEXT_WINDOW,
    VALID_PERMISSION_MODES,
    VALID_PROTOCOLS,
    VALID_TEAMMATE_MODES,
    lookup_model_context_window,
    validate_config_structure,
)


_ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compat": "OPENAI_API_KEY",
}

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class ProviderConfig:
    name: str
    protocol: str
    base_url: str
    model: str
    api_key: str = ""
    thinking: bool = False
    # 0 表示"未设置" — get_context_window() 通过四层 fallback 解析真实窗口大小。
    # 正数表示配置文件里显式指定的覆盖值。
    context_window: int = 0
    max_output_tokens: int = 0
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    # 缓存读/写单价。None 表示未配置 —— UsageLedger 会回退到 input 单价。
    # 分开配置可避免在 cache-heavy 场景用 input 单价高估成本。
    cache_read_cost_per_million: float | None = None
    cache_write_cost_per_million: float | None = None
    # 运行时 cache，存放从 provider 的 /v1/models 端点自动拉取的 context window
    # （get_context_window 的第 2 层）。通过 set_fetched_context_window() 写入一次；
    # 0 表示"尚未拉取"。不会持久化。
    _fetched_context_window: int = field(default=0, repr=False)

    def resolve_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        env_var = _ENV_KEY_MAP.get(self.protocol, "")
        return os.environ.get(env_var, "")

    def set_fetched_context_window(self, window: int) -> None:
        """记录从 provider 自动拉取到的 context window（第 2 层）。

        非正数会被忽略，这样一次失败的拉取就不会污染 cache。在解析
        context window 时，每个 provider 只会调用一次。
        """
        if window > 0:
            self._fetched_context_window = window

    def get_context_window(self) -> int:
        """通过四层 fallback 解析模型的 context window，按优先级从高到低：

          1. 配置文件提供的 context_window（> 0）——显式覆盖，永远优先。
          2. 从 provider 的 /v1/models 端点自动拉取并通过 set_fetched_context_window
             缓存的值（只有 anthropic 协议的 provider 才会设置它；拉取失败或缺失时
             保持为 0 并跳过）。
          3. 内置的「模型名 -> window」映射表（按子串匹配）。
          4. 保守的默认值（claude -> 200000，其他 -> 128000）。
        """
        if self.context_window > 0:
            return self.context_window
        if self._fetched_context_window > 0:
            return self._fetched_context_window
        window = lookup_model_context_window(self.model)
        if window > 0:
            return window
        if "claude" in self.model.lower():
            return DEFAULT_CONTEXT_WINDOW
        return 128_000

    def get_max_output_tokens(self) -> int:
        if self.max_output_tokens > 0:
            return self.max_output_tokens
        capabilities = resolve_model_capabilities(self.model, self.protocol)
        if self.thinking:
            return max(capabilities.default_max_output_tokens, 64_000)
        return min(capabilities.default_max_output_tokens, 8192)


def resolve_env_vars(value: str) -> str:
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def build_child_env(declared_env: dict[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.environ.get("PATH", "")
    if path:
        env["PATH"] = path
    for key, value in (declared_env or {}).items():
        env[key] = resolve_env_vars(value)
    return env


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


    @property
    def is_stdio(self) -> bool:
        return self.command is not None


@dataclass
class WorktreeConfig:
    symlink_directories: list[str] = field(default_factory=lambda: ["node_modules", ".venv", "vendor"])
    stale_cleanup_interval: int = 3600
    stale_cutoff_hours: int = 24


@dataclass
class SandboxAppConfig:
    """沙箱相关的配置项。"""
    enabled: bool = False         # 是否启用 OS 级沙箱
    auto_allow: bool = False      # 是否自动放行命令（沙箱兜底）
    network_enabled: bool = False  # 沙箱内是否允许网络访问


@dataclass
class AppConfig:
    providers: list[ProviderConfig]
    permission_mode: str = "default"
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    raw_hooks: list[dict] = field(default_factory=list)
    enable_fork: bool = False
    enable_verification_agent: bool = False
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    teammate_mode: str = ""
    enable_coordinator_mode: bool = False
    sandbox: SandboxAppConfig = field(default_factory=SandboxAppConfig)
    run_limits: RunLimits = field(default_factory=RunLimits)
    _explicit_fields: frozenset[str] = field(default_factory=frozenset, repr=False)
    _explicit_worktree_fields: frozenset[str] = field(default_factory=frozenset, repr=False)
    _explicit_sandbox_fields: frozenset[str] = field(default_factory=frozenset, repr=False)
    _explicit_run_limit_fields: frozenset[str] = field(default_factory=frozenset, repr=False)


def _load_single_file(path: Path, *, require_providers: bool = True) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse config {path}: {e}") from e

    validated = validate_config_structure(raw, require_providers=require_providers)
    assert isinstance(raw, dict)

    providers = [
        ProviderConfig(
            name=p["name"],
            protocol=p["protocol"],
            base_url=p["base_url"],
            model=p["model"],
            api_key=p["api_key"],
            thinking=p["thinking"],
            context_window=p["context_window"],
            max_output_tokens=p["max_output_tokens"],
            input_cost_per_million=p["input_cost_per_million"],
            output_cost_per_million=p["output_cost_per_million"],
            cache_read_cost_per_million=p["cache_read_cost_per_million"],
            cache_write_cost_per_million=p["cache_write_cost_per_million"],
        )
        for p in validated["providers"]
    ]

    mcp_servers = [
        MCPServerConfig(
            name=s["name"],
            command=s["command"],
            args=s["args"],
            url=s["url"],
            headers=s["headers"],
            env=s["env"],
        )
        for s in validated["mcp_servers"]
    ]

    wt = validated["worktree"]
    worktree_cfg = WorktreeConfig(
        symlink_directories=wt["symlink_directories"],
        stale_cleanup_interval=wt["stale_cleanup_interval"],
        stale_cutoff_hours=wt["stale_cutoff_hours"],
    )

    sb = validated["sandbox"]
    sandbox_cfg = SandboxAppConfig(
        enabled=sb["enabled"],
        auto_allow=sb["auto_allow"],
        network_enabled=sb["network_enabled"],
    )
    limits = validated["run_limits"]
    run_limits = RunLimits(
        max_turns=limits["max_turns"],
        max_wall_time_seconds=limits["max_wall_time_seconds"],
        max_total_tokens=limits["max_total_tokens"],
        max_cost_usd=limits["max_cost_usd"],
    )

    return AppConfig(
        providers=providers,
        permission_mode=validated["permission_mode"],
        mcp_servers=mcp_servers,
        raw_hooks=validated["hooks"],
        enable_fork=validated["enable_fork"],
        enable_verification_agent=validated["enable_verification_agent"],
        worktree=worktree_cfg,
        teammate_mode=validated["teammate_mode"],
        enable_coordinator_mode=validated["enable_coordinator_mode"],
        sandbox=sandbox_cfg,
        run_limits=run_limits,
        _explicit_fields=frozenset(raw),
        _explicit_worktree_fields=frozenset(
            raw.get("worktree", {}) if isinstance(raw.get("worktree"), dict) else {}
        ),
        _explicit_sandbox_fields=frozenset(
            raw.get("sandbox", {}) if isinstance(raw.get("sandbox"), dict) else {}
        ),
        _explicit_run_limit_fields=frozenset(
            raw.get("run_limits", {})
            if isinstance(raw.get("run_limits"), dict)
            else {}
        ),
    )


def _merge_config(base: AppConfig, override: AppConfig) -> AppConfig:
    # 深拷贝 base，避免原地修改调用方传入的对象（C-6）
    base = copy.deepcopy(base)
    explicit = override._explicit_fields
    if "providers" in explicit:
        base.providers = override.providers
    if "permission_mode" in explicit:
        base.permission_mode = override.permission_mode

    if "mcp_servers" in explicit:
        if not override.mcp_servers:
            base.mcp_servers = []
        else:
            by_name = {s.name: i for i, s in enumerate(base.mcp_servers)}
            for server in override.mcp_servers:
                if server.name in by_name:
                    base.mcp_servers[by_name[server.name]] = server
                else:
                    base.mcp_servers.append(server)
                    by_name[server.name] = len(base.mcp_servers) - 1

    if "hooks" in explicit:
        if override.raw_hooks:
            base.raw_hooks.extend(override.raw_hooks)
        else:
            base.raw_hooks = []
    if "enable_fork" in explicit:
        base.enable_fork = override.enable_fork
    if "enable_verification_agent" in explicit:
        base.enable_verification_agent = override.enable_verification_agent
    if "teammate_mode" in explicit:
        base.teammate_mode = override.teammate_mode
    if "enable_coordinator_mode" in explicit:
        base.enable_coordinator_mode = override.enable_coordinator_mode

    for field_name in override._explicit_worktree_fields:
        setattr(base.worktree, field_name, getattr(override.worktree, field_name))
    for field_name in override._explicit_sandbox_fields:
        setattr(base.sandbox, field_name, getattr(override.sandbox, field_name))
    if override._explicit_run_limit_fields:
        base.run_limits = replace(
            base.run_limits,
            **{
                field_name: getattr(override.run_limits, field_name)
                for field_name in override._explicit_run_limit_fields
            },
        )
    return base


def load_config(
    path: Path | None = None,
    *,
    include_project: bool = True,
    cwd: Path | None = None,
) -> AppConfig:
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return _load_single_file(path, require_providers=True)

    cwd = (cwd or Path.cwd()).expanduser().resolve()
    home = Path.home()
    candidates = [home / ".myclaude" / "config.yaml"]
    if include_project:
        candidates.extend(
            [
                cwd / ".myclaude" / "config.yaml",
                cwd / ".myclaude" / "config.local.yaml",
            ]
        )

    merged = AppConfig(providers=[])
    found = False
    for p in candidates:
        if not p.exists():
            continue
        found = True
        layer = _load_single_file(p, require_providers=False)
        merged = _merge_config(merged, layer)

    if not found:
        raise ConfigError(
            "No trusted config file found. Expected ~/.myclaude/config.yaml"
            + (" or .myclaude/config.yaml in the project" if include_project else "")
        )
    if not merged.providers:
        raise ConfigError("At least one provider must be configured across config layers")
    return merged

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


ThinkingMode = Literal["none", "enabled", "adaptive"]


@dataclass(frozen=True)
class ModelCapabilities:
    """Provider-facing model behavior used by config and request builders.

    Explicit provider configuration still wins.  This registry only supplies
    conservative defaults and keeps model-name heuristics in one place.
    """

    context_window: int
    default_max_output_tokens: int = 8192
    thinking_mode: ThinkingMode = "none"


@dataclass(frozen=True)
class _CapabilityRule:
    pattern: re.Pattern[str]
    context_window: int
    default_max_output_tokens: int = 8192
    thinking_mode: ThinkingMode = "none"


_RULES: tuple[_CapabilityRule, ...] = (
    _CapabilityRule(
        re.compile(r"claude-(?:opus|sonnet)-4(?:[.-]?6)(?:-|$)", re.IGNORECASE),
        context_window=200_000,
        default_max_output_tokens=64_000,
        thinking_mode="adaptive",
    ),
    _CapabilityRule(
        re.compile(r"claude", re.IGNORECASE),
        context_window=200_000,
        default_max_output_tokens=64_000,
        thinking_mode="enabled",
    ),
    _CapabilityRule(re.compile(r"(?:^|[-_.])1m(?:$|[-_.])", re.IGNORECASE), 1_000_000),
    _CapabilityRule(re.compile(r"gpt-4\.1", re.IGNORECASE), 1_000_000),
    _CapabilityRule(re.compile(r"gpt-4o", re.IGNORECASE), 128_000),
    _CapabilityRule(re.compile(r"gpt-4-turbo", re.IGNORECASE), 128_000),
    _CapabilityRule(re.compile(r"(?:^|[-_.])o[134](?:$|[-_.])", re.IGNORECASE), 200_000),
    _CapabilityRule(re.compile(r"gpt-3\.5", re.IGNORECASE), 16_385),
)


def lookup_model_capabilities(model: str) -> ModelCapabilities | None:
    """Return the first explicit family match, or ``None`` when unknown."""

    # A 1M suffix is an explicit context override for otherwise known families.
    one_million = bool(re.search(r"(?:^|[-_.])1m(?:$|[-_.])", model, re.IGNORECASE))
    matched: _CapabilityRule | None = None
    for rule in _RULES:
        if rule.pattern.search(model):
            matched = rule
            if "claude" in model.lower():
                break
            if not one_million:
                break
    if matched is None:
        return None
    return ModelCapabilities(
        context_window=1_000_000 if one_million else matched.context_window,
        default_max_output_tokens=matched.default_max_output_tokens,
        thinking_mode=matched.thinking_mode,
    )


def resolve_model_capabilities(model: str, protocol: str) -> ModelCapabilities:
    matched = lookup_model_capabilities(model)
    if matched is not None:
        return matched
    return ModelCapabilities(
        context_window=200_000 if protocol == "anthropic" else 128_000,
        default_max_output_tokens=8192,
        thinking_mode="enabled" if protocol == "anthropic" else "none",
    )


def supports_anthropic_tool_search(model: str) -> bool:
    """Return whether Anthropic documents native tool search for this model."""
    normalized = model.casefold()
    if re.search(r"claude-(?:fable|mythos)-5(?:-|$)", normalized):
        return True
    match = re.search(
        r"claude-(?:opus|sonnet|haiku)-4[-.]?(\d+)(?:-|$)", normalized
    )
    return match is not None and int(match.group(1)) >= 5

"""LLM providers and the factory that builds one from ``hub.yaml``'s ``llm``
section (docs/ai-harness/SITE.md)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from .base import LlmError, LlmEvent, LlmProvider, ToolDef
from .scripted import ScriptedProvider
from .zai import ZaiProvider

__all__ = ["DisabledProvider", "ScriptedProvider", "ZaiProvider", "make_provider"]

logger = logging.getLogger(__name__)

_COMMON_KEYS = frozenset({"provider", "model"})
_ZAI_KEYS = frozenset(
    {
        "base_url",
        "api_key",
        "api_key_env",
        "reasoning_effort",
        "max_tokens",
        "temperature",
        "thinking",
        "clear_thinking",
        "timeout_s",
        "connect_timeout_s",
        "max_retries",
    }
)
_SCRIPTED_KEYS = frozenset({"script", "delay_s", "chunk_size"})


class DisabledProvider(LlmProvider):
    """``provider: none``: the hub and its tools run, the agent cannot."""

    name = "none"
    model = ""
    configured = False

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDef],
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LlmEvent]:
        raise LlmError("the AI agent is off for this hub (llm.provider: none)")
        yield  # pragma: no cover - makes this an async generator like the others


def make_provider(
    config: Mapping[str, Any] | None, *, base_dir: str | Path | None = None
) -> LlmProvider:
    """Build the provider named by ``config["provider"]`` (zai | scripted | none).

    ``base_dir`` resolves a relative ``script`` path (the directory of
    ``hub.yaml``). Keys meant for another provider are ignored, so one
    ``llm`` section can be switched between providers by changing one line.
    Raises ``ValueError`` for an unknown provider or an invalid value.
    """
    if config is not None and not isinstance(config, Mapping):
        raise ValueError(f"llm must be a mapping with a provider key, not {type(config).__name__}")
    cfg = dict(config or {})
    kind = str(cfg.get("provider") or "none").strip().lower()
    unknown = set(cfg) - _COMMON_KEYS - _ZAI_KEYS - _SCRIPTED_KEYS
    if unknown:
        logger.warning("llm: ignoring unknown settings %s", sorted(unknown))

    if kind == "none":
        return DisabledProvider()
    if kind == "zai":
        options: dict[str, Any] = {
            "model": _str(cfg, "model"),
            "base_url": _str(cfg, "base_url"),
            "api_key": _str(cfg, "api_key"),
            "api_key_env": _str(cfg, "api_key_env"),
            "reasoning_effort": _str(cfg, "reasoning_effort"),
            "max_tokens": _int(cfg, "max_tokens"),
            "temperature": _float(cfg, "temperature"),
            "thinking": _bool(cfg, "thinking"),
            "clear_thinking": _bool(cfg, "clear_thinking"),
            "timeout_s": _float(cfg, "timeout_s"),
            "connect_timeout_s": _float(cfg, "connect_timeout_s"),
            "max_retries": _int(cfg, "max_retries"),
        }
        return ZaiProvider(**{k: v for k, v in options.items() if v is not None})
    if kind == "scripted":
        overrides = {
            "model": _str(cfg, "model"),
            "delay_s": _float(cfg, "delay_s"),
            "chunk_size": _int(cfg, "chunk_size"),
        }
        script = cfg.get("script")
        if script is None or script == "demo":
            return ScriptedProvider.demo(**overrides)
        if isinstance(script, (Mapping, list)):
            return ScriptedProvider.from_dict(script, **overrides)
        if isinstance(script, (str, Path)):
            path = Path(script).expanduser()
            if not path.is_absolute() and base_dir is not None:
                path = Path(base_dir) / path
            return ScriptedProvider.from_file(path, **overrides)
        raise ValueError("llm.script must be a path, an inline script or null")
    raise ValueError(f"unknown llm provider {kind!r} (expected zai, scripted or none)")


def _str(cfg: Mapping[str, Any], key: str) -> str | None:
    value = cfg.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"llm.{key} must be a string")
    return value.strip() or None


def _float(cfg: Mapping[str, Any], key: str) -> float | None:
    value = cfg.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"llm.{key} must be a number")
    return float(value)


def _int(cfg: Mapping[str, Any], key: str) -> int | None:
    value = _float(cfg, key)
    if value is None:
        return None
    if not value.is_integer():
        raise ValueError(f"llm.{key} must be a whole number")
    return int(value)


def _bool(cfg: Mapping[str, Any], key: str) -> bool | None:
    value = cfg.get(key)
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"llm.{key} must be true or false")

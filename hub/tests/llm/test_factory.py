from __future__ import annotations

import logging
from pathlib import Path

import pytest

from uc_hub.llm import DisabledProvider, ScriptedProvider, ZaiProvider, make_provider
from uc_hub.llm.base import LlmError


async def test_zai_from_hub_yaml_section() -> None:
    provider = make_provider(
        {
            "provider": "zai",
            "model": "glm-5.3",
            "base_url": "https://api.z.ai/api/paas/v4",
            "api_key_env": "UC_TEST_FACTORY_KEY",
            "reasoning_effort": "high",
            "max_tokens": 8192,
            "timeout_s": 120,
            "script": None,
        }
    )
    try:
        assert isinstance(provider, ZaiProvider)
        assert (provider.name, provider.model, provider.api_key_env) == (
            "zai",
            "glm-5.3",
            "UC_TEST_FACTORY_KEY",
        )
        assert (provider.reasoning_effort, provider.max_tokens, provider.timeout_s) == (
            "high",
            8192,
            120.0,
        )
        body = provider.build_request([{"role": "user", "content": "x"}], [])
        assert body["reasoning_effort"] == "high" and body["max_tokens"] == 8192
    finally:
        await provider.aclose()


async def test_zai_defaults() -> None:
    provider = make_provider({"provider": "ZAI"})
    try:
        assert isinstance(provider, ZaiProvider)
        assert (provider.model, provider.base_url, provider.api_key_env, provider.max_retries) == (
            "glm-5.3",
            "https://api.z.ai/api/paas/v4",
            "ZAI_API_KEY",
            3,
        )
    finally:
        await provider.aclose()


def test_scripted_default_is_the_demo() -> None:
    provider = make_provider({"provider": "scripted", "delay_s": 0})
    assert isinstance(provider, ScriptedProvider)
    assert provider.model == "scripted-demo" and provider.delay_s == 0
    assert any(rule.id == "commission-search" for rule in provider.rules)


def test_scripted_from_relative_path_and_inline(tmp_path: Path) -> None:
    (tmp_path / "s.yaml").write_text(
        "rules: [{when: {always: true}, respond: {text: hi}}]\n", encoding="utf-8"
    )
    provider = make_provider({"provider": "scripted", "script": "s.yaml"}, base_dir=tmp_path)
    assert isinstance(provider, ScriptedProvider) and len(provider.rules) == 1
    inline = make_provider(
        {
            "provider": "scripted",
            "model": "fake",
            "chunk_size": 3,
            "script": [{"when": {"always": True}, "respond": {"text": "hi"}}],
        }
    )
    assert isinstance(inline, ScriptedProvider)
    assert (inline.model, inline.chunk_size) == ("fake", 3)


@pytest.mark.parametrize("config", [None, {}, {"provider": "none"}, {"provider": None}])
async def test_none_raises_on_use(config: dict[str, object] | None) -> None:
    provider = make_provider(config)
    assert isinstance(provider, DisabledProvider)
    assert not provider.configured and provider.name == "none"
    with pytest.raises(LlmError, match="off"):
        async for _ in provider.stream([{"role": "user", "content": "hi"}], []):
            pass


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"provider": "openai"}, "unknown llm provider"),
        ({"provider": "zai", "max_tokens": "lots"}, "max_tokens must be a number"),
        ({"provider": "zai", "max_tokens": 1.5}, "whole number"),
        ({"provider": "zai", "timeout_s": 0}, "positive"),
        ({"provider": "zai", "clear_thinking": "no"}, "true or false"),
        ({"provider": "zai", "model": 5.3}, "must be a string"),
        ({"provider": "scripted", "script": 7}, "llm.script"),
        ({"provider": "scripted", "script": "/nonexistent/script.yaml"}, "cannot read script"),
    ],
)
def test_invalid_config(config: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make_provider(config)


@pytest.mark.parametrize("config", ["zai", ["zai"], 5])
def test_llm_section_must_be_a_mapping(config: object) -> None:
    """``llm: zai`` in hub.yaml is a plausible slip; say what is wrong."""
    with pytest.raises(ValueError, match="llm must be a mapping"):
        make_provider(config)  # type: ignore[arg-type]


def test_unknown_keys_are_reported(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="uc_hub.llm")
    make_provider({"provider": "none", "api_kye_env": "X"})
    assert "api_kye_env" in caplog.text

"""LLM provider contract.

The agent loop sends OpenAI-style chat messages and tool definitions and
consumes a stream of ``LlmEvent``s. Providers translate their wire format
(Z.ai's OpenAI-compatible SSE, a scripted fake, ...) into these events.

Messages use the OpenAI chat format:
    {"role": "system"|"user"|"assistant"|"tool", "content": str, ...}
    assistant messages may carry "tool_calls": [{"id", "type": "function",
        "function": {"name", "arguments": <json string>}}]
    and "reasoning_content": str (GLM thinking, sent back on later turns)
    tool messages carry "tool_call_id".
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal


@dataclass(slots=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    #: Raw JSON text as produced by the model (may be invalid JSON).
    arguments: str


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


# Stream events --------------------------------------------------------------
@dataclass(slots=True)
class ThinkingDelta:
    text: str
    type: Literal["thinking"] = "thinking"


@dataclass(slots=True)
class TextDelta:
    text: str
    type: Literal["text"] = "text"


@dataclass(slots=True)
class ToolCallStart:
    """Emitted as soon as a tool call's id and name are known."""

    index: int
    id: str
    name: str
    type: Literal["tool_start"] = "tool_start"


@dataclass(slots=True)
class ToolCallDelta:
    """A fragment of a tool call's argument JSON (Z.ai ``tool_stream``)."""

    index: int
    arguments_delta: str
    type: Literal["tool_delta"] = "tool_delta"


@dataclass(slots=True)
class Completed:
    """Last event of every successful stream: the full assistant turn."""

    text: str
    reasoning: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # stop | tool_calls | length | sensitive | ...
    usage: Usage = field(default_factory=Usage)
    type: Literal["completed"] = "completed"


LlmEvent = ThinkingDelta | TextDelta | ToolCallStart | ToolCallDelta | Completed


class LlmError(Exception):
    """Provider failure after retries. ``retryable`` is informational."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LlmProvider(abc.ABC):
    name: str
    model: str

    @abc.abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDef],
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LlmEvent]:
        """Yield events, ending with exactly one ``Completed``; raise
        ``LlmError`` on failure."""

    async def aclose(self) -> None:
        return None

"""Request bodies: parsed and checked here instead of by FastAPI, so every
problem is a 400 in the API's error shape. JSON is strict (no NaN or
Infinity), the body must be an object, and every string must be valid
Unicode: a lone surrogate (legal in JSON escapes) could not be stored,
logged or sent on, so it is refused up front."""

from __future__ import annotations

import json
from typing import Any, Literal, TypeVar

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..core.errors import InvalidRequest

T = TypeVar("T", bound="Body")

MAX_BODY_BYTES = 1 << 20


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadPoints(Body):
    ids: list[str] = Field(min_length=1, max_length=500)


class Discover(Body):
    protocol: Literal["bacnet-uc", "bacnet-ip", "mqtt"] | None = None
    timeout_s: float = Field(default=3.0, gt=0, le=30)


class Identify(Body):
    seconds: int = Field(default=30, ge=1, le=3600)


class CreateRun(Body):
    message: str
    playbook: str | None = None


class Message(Body):
    message: str


class Answer(Body):
    question_id: str = Field(min_length=1, max_length=64)
    answer: str = Field(max_length=2000)


class Decide(Body):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)
    scope: Literal["call", "run"] = "call"


async def parse(request: Request, model: type[T]) -> T:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise InvalidRequest(f"the request body is larger than {MAX_BODY_BYTES} bytes")
    try:
        data = json.loads(raw, parse_constant=_no_constant) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError) as e:
        raise InvalidRequest(f"the request body is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise InvalidRequest("the request body must be a JSON object")
    _check_text(data)
    try:
        return model.model_validate(data)
    except ValidationError as e:
        errors = [f"{'.'.join(str(p) for p in err['loc']) or '<body>'}: {err['msg']}" for err in e.errors()]
        raise InvalidRequest("invalid request body: " + "; ".join(errors)) from None


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON value")


def _check_text(value: Any, path: str = "") -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidRequest(f"{path or 'a string'} is not valid Unicode text (a lone surrogate)") from None
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_text(key, path)
            _check_text(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_text(item, f"{path}[{index}]")

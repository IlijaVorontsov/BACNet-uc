"""``hub.yaml``: how this gateway runs (docs/ai-harness/SITE.md).

Unknown keys are refused, so a misspelt setting fails at startup instead of
being ignored. Relative paths (``site_file``, ``data_dir``, ``web_dir``, the
MQTT TLS files, ``drivers.bacnet_uc.uc_link_wasm``, and ``llm.script``
through ``make_provider(base_dir=...)``) are relative to the directory of
hub.yaml. Secrets come from literal values or ``*_env`` variables only;
they are held as ``SecretStr`` and never appear in a repr, a log line or an
error message. Without auth tokens (dev mode, every request is an admin)
the hub only listens on a loopback address, unless ``--insecure-listen``
(``insecure_listen``) says otherwise.

The driver sections are passed to the drivers as ``DriverContext.settings``
with only the keys the file sets, so each driver keeps its own defaults and
range checks.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    model_validator,
)

from ..core.errors import ValidationFailed
from ..core.types import ProtocolName
from ..manifest.load import parse_yaml

Role = Literal["viewer", "operator", "commissioner", "admin"]


def _resolve(value: Path | None, info: ValidationInfo) -> Path | None:
    if value is None:
        return None
    value = value.expanduser()
    base = (info.context or {}).get("base_dir")
    if not value.is_absolute() and base is not None:
        value = Path(base) / value
    return value


#: A path relative to the directory of hub.yaml.
ConfigPath = Annotated[Path, AfterValidator(_resolve)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def settings(self) -> dict[str, Any]:
        """The keys the file sets, as plain values (paths as text, secrets revealed)."""
        return {k: _plain(getattr(self, k)) for k in self.model_fields_set}


def _plain(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, _Section):
        return value.settings()
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


class ListenConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=0, le=65535)


@dataclass(frozen=True, slots=True)
class Identity:
    user: str
    roles: frozenset[str]


class TokenConfig(_Section):
    user: str = Field(min_length=1, max_length=64)
    roles: list[Role] = Field(min_length=1)
    token: SecretStr | None = None
    token_env: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> TokenConfig:
        if (self.token is None) == (self.token_env is None):
            raise ValueError(f"auth token of {self.user!r}: give exactly one of token and token_env")
        return self


class AuthConfig(_Section):
    tokens: list[TokenConfig] = Field(default_factory=list)

    @property
    def dev_mode(self) -> bool:
        """No tokens: every request is user ``dev`` with all roles; ``HubConfig``
        then refuses a ``listen.host`` that is not a loopback address."""
        return not self.tokens

    def identities(self, environ: Mapping[str, str] | None = None) -> dict[str, Identity]:
        """Bearer token -> identity. Raises ``ValidationFailed`` for a missing
        environment variable, an empty token or a token given twice."""
        env = os.environ if environ is None else environ
        out: dict[str, Identity] = {}
        errors: list[str] = []
        for entry in self.tokens:
            if entry.token_env is not None:
                token = env.get(entry.token_env, "").strip()
                where = f"the environment variable {entry.token_env}"
            else:
                assert entry.token is not None
                token = entry.token.get_secret_value().strip()
                where = "auth.tokens[].token"
            if not token:
                errors.append(f"auth token of {entry.user!r}: {where} is empty or not set")
            elif token in out:
                errors.append(f"auth token of {entry.user!r} is also the token of {out[token].user!r}")
            else:
                out[token] = Identity(entry.user, frozenset(entry.roles))
        if errors:
            raise ValidationFailed("hub.yaml: invalid auth tokens", errors)
        return out


class LlmConfig(_Section):
    provider: Literal["zai", "scripted", "none"] = "none"
    model: str | None = None
    # zai
    base_url: str | None = None
    api_key: SecretStr | None = None
    api_key_env: str | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = None
    thinking: bool | None = None
    clear_thinking: bool | None = None
    timeout_s: float | None = Field(default=None, gt=0)
    connect_timeout_s: float | None = Field(default=None, gt=0)
    max_retries: int | None = Field(default=None, ge=0)
    # scripted: a path relative to hub.yaml (resolved by make_provider), "demo" or an inline script
    script: str | dict[str, Any] | list[Any] | None = None
    delay_s: float | None = Field(default=None, ge=0)
    chunk_size: int | None = Field(default=None, ge=1)


class AgentConfig(_Section):
    max_tool_calls: int = Field(default=60, ge=1)
    max_wall_s: float = Field(default=1200.0, gt=0)
    #: Bytes of a tool result sent to the model before it becomes a handle.
    result_inline_limit: int = Field(default=4000, ge=512)


class McpConfig(_Section):
    """``uc-hub mcp``: the identity MCP clients act as. Their tier L and C
    calls wait up to ``approval_wait_s`` for a person to decide in the web app."""

    user: str = Field(default="mcp", min_length=1, max_length=64)
    roles: list[Role] = Field(default=["operator"], min_length=1)
    approval_wait_s: float = Field(default=600.0, gt=0)


class BacnetUcConfig(_Section):
    timeout_s: float | None = None
    retries: int | None = None
    mtu: int | None = None
    max_inflight: int | None = None
    objects_page: int | None = None
    poll_interval_s: float | None = None
    refresh_s: float | None = None
    heartbeat_s: float | None = None
    read_concurrency: int | None = None
    units_cache_s: float | None = None
    discover_broadcast: str | list[str] | None = None
    #: Node name -> "host:port" for ``transport: sim`` nodes.
    sim_addresses: dict[str, str] | None = None
    #: The stock uc-link module; links are skipped with a warning without it.
    uc_link_wasm: ConfigPath | None = None

    def settings(self) -> dict[str, Any]:
        out = super().settings()
        out.pop("uc_link_wasm", None)
        return out


class BacnetIpConfig(_Section):
    interface: str | None = None
    port: int | None = None
    device_instance: int | None = None
    device_name: str | None = None
    #: ``null`` disables the global Who-Is (unlike leaving the key out).
    broadcast: str | None = None
    discover_targets: list[str] | None = None
    vendor_identifier: int | None = None
    timeout_s: float | None = None
    retries: int | None = None
    poll_interval_s: float | None = None
    refresh_s: float | None = None
    cov: bool | None = None
    cov_lifetime_s: int | None = None
    cov_confirmed: bool | None = None
    cov_process_id: int | None = None
    max_objects: int | None = None
    rpm_max_properties: int | None = None
    device_concurrency: int | None = None


class MqttTlsConfig(_Section):
    ca: ConfigPath | None = None
    cert: ConfigPath | None = None
    key: ConfigPath | None = None
    insecure: bool | None = None


class MqttConfig(_Section):
    host: str | None = None
    port: int | None = None
    #: A mapping, ``true`` (system trust store) or left out for plain MQTT.
    tls: MqttTlsConfig | bool | None = None
    username: str | None = None
    password: SecretStr | None = None
    password_env: str | None = None
    client_id: str | None = None
    keepalive_s: int | None = None
    timeout_s: float | None = None
    reconnect_min_s: float | None = None
    reconnect_max_s: float | None = None
    stale_after_s: float | None = None
    command_timeout_s: float | None = None
    max_payload_bytes: int | None = None

    @model_validator(mode="after")
    def _one_password(self) -> MqttConfig:
        if self.password is not None and self.password_env is not None:
            raise ValueError("drivers.mqtt: give password or password_env, not both")
        return self


class DriversConfig(_Section):
    bacnet_uc: BacnetUcConfig | None = None
    bacnet_ip: BacnetIpConfig | None = None
    mqtt: MqttConfig | None = None


_SECTIONS = {
    ProtocolName.BACNET_UC: "bacnet_uc",
    ProtocolName.BACNET_IP: "bacnet_ip",
    ProtocolName.MQTT: "mqtt",
}


class HubConfig(_Section):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    #: Directory of hub.yaml; relative paths were resolved against it.
    base_dir: Path
    site_file: ConfigPath = Path("site.yaml")
    #: SQLite database (``uc-hub.db``) and result blobs.
    data_dir: ConfigPath = Path("data")
    listen: ListenConfig = ListenConfig()
    web_dir: ConfigPath | None = None
    auth: AuthConfig = AuthConfig()
    llm: LlmConfig = LlmConfig()
    agent: AgentConfig = AgentConfig()
    mcp: McpConfig = McpConfig()
    drivers: DriversConfig = DriversConfig()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, base_dir: Path | str, *,
                  insecure_listen: bool = False) -> HubConfig:
        """Validate a parsed hub.yaml; ``ValidationFailed`` lists every problem.
        ``insecure_listen`` (the ``--insecure-listen`` flag) lets dev mode
        listen on a non-loopback address."""
        raw = dict(data or {})
        if "base_dir" in raw:
            raise ValidationFailed("hub.yaml is invalid", ["base_dir: not a setting (it is where hub.yaml is)"])
        base = Path(base_dir).expanduser().resolve()
        try:
            config = cls.model_validate({**raw, "base_dir": base}, context={"base_dir": base})
        except ValidationError as e:
            raise ValidationFailed("hub.yaml is invalid", [_error_text(err) for err in e.errors()]) from None
        # Dev mode makes every request an admin; only this machine may make them.
        if config.auth.dev_mode and not insecure_listen and not _loopback(config.listen.host):
            raise ValidationFailed("hub.yaml is invalid", [
                "listen.host: without auth.tokens (dev mode) the hub listens on a loopback address only "
                "(127.0.0.1, ::1 or localhost)"])
        return config

    @property
    def db_path(self) -> Path:
        return self.data_dir / "uc-hub.db"

    @property
    def uc_link_wasm(self) -> Path | None:
        section = self.drivers.bacnet_uc
        return section.uc_link_wasm if section is not None else None

    def configured_protocols(self) -> set[ProtocolName]:
        """Protocols with a ``drivers`` section; their drivers run even when
        the manifest has no device of that protocol (for discovery)."""
        return {p for p, key in _SECTIONS.items() if getattr(self.drivers, key) is not None}

    def driver_settings(self, protocol: ProtocolName) -> dict[str, Any]:
        """``DriverContext.settings`` for a protocol's driver."""
        section = getattr(self.drivers, _SECTIONS[protocol])
        return section.settings() if section is not None else {}

    def llm_settings(self) -> dict[str, Any]:
        """The ``llm`` section for ``llm.make_provider`` (with ``base_dir``)."""
        return self.llm.settings()


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _error_text(error: Any) -> str:
    """A pydantic error without the input value, which may be a secret."""
    where = ".".join(str(p) for p in error.get("loc", ())) or "<root>"
    return f"{where}: {error.get('msg', 'invalid')}"


def load_config(path: Path | str, *, insecure_listen: bool = False,
                overrides: Mapping[str, Any] | None = None) -> HubConfig:
    """Read and validate hub.yaml (same restricted YAML loader as site.yaml).
    ``overrides`` (command line options) are merged into the file's
    mappings before validation, so they are checked the same way."""
    path = Path(path).expanduser().resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValidationFailed(f"cannot read {path}: {e.strerror or e}") from None
    except UnicodeDecodeError as e:
        raise ValidationFailed(f"{path.name} is not UTF-8 text", [str(e)]) from None
    data = parse_yaml(text, path.name)
    if data is not None and not isinstance(data, Mapping):
        raise ValidationFailed(f"{path.name} must be a mapping")
    if overrides:
        data = _merge(data or {}, overrides)
    return HubConfig.from_dict(data, path.parent, insecure_listen=insecure_listen)


def _merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        current = out.get(key)
        out[key] = _merge(current, value) if isinstance(value, Mapping) and isinstance(current, Mapping) else value
    return out

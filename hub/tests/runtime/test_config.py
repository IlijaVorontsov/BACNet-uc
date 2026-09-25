"""hub.yaml: validation, path resolution, driver settings and secrets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from uc_hub.core.errors import ValidationFailed
from uc_hub.core.types import ProtocolName
from uc_hub.llm import DisabledProvider, ScriptedProvider, make_provider
from uc_hub.runtime import HubConfig, Identity, load_config


def write(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / "conf" / "hub.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))
    return path


def test_defaults_resolve_against_the_hub_yaml_directory(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, {}))
    base = (tmp_path / "conf").resolve()
    assert config.base_dir == base
    assert config.site_file == base / "site.yaml"
    assert config.db_path == base / "data" / "uc-hub.db"
    assert (config.listen.host, config.listen.port, config.web_dir) == ("127.0.0.1", 8080, None)
    assert (config.llm.provider, config.agent.max_tool_calls, config.agent.result_inline_limit) == ("none", 60, 4000)
    assert config.auth.dev_mode
    assert config.configured_protocols() == set()
    assert config.driver_settings(ProtocolName.MQTT) == {}
    assert config.uc_link_wasm is None


def test_every_relative_path_is_resolved(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, {
        "site_file": "sites/hq.yaml",
        "data_dir": "../var",
        "web_dir": "/srv/web",
        "drivers": {
            "bacnet_uc": {"timeout_s": 1.5, "uc_link_wasm": "wasm/uc-link.wasm", "sim_addresses": {"r204-ctl": "h:1"}},
            "mqtt": {"host": "broker", "tls": {"ca": "certs/ca.crt", "cert": "~/client.crt", "key": "certs/c.key"}},
        },
    }))
    base = (tmp_path / "conf").resolve()
    assert config.site_file == base / "sites" / "hq.yaml"
    assert config.data_dir == base / "../var"
    assert config.web_dir == Path("/srv/web")
    assert config.uc_link_wasm == base / "wasm" / "uc-link.wasm"
    assert config.driver_settings(ProtocolName.BACNET_UC) == {"timeout_s": 1.5, "sim_addresses": {"r204-ctl": "h:1"}}
    mqtt = config.driver_settings(ProtocolName.MQTT)
    assert mqtt["tls"] == {"ca": str(base / "certs" / "ca.crt"), "cert": str(Path.home() / "client.crt"),
                           "key": str(base / "certs" / "c.key")}
    assert config.configured_protocols() == {ProtocolName.BACNET_UC, ProtocolName.MQTT}


def test_driver_settings_pass_only_what_the_file_sets(tmp_path: Path) -> None:
    config = HubConfig.from_dict({"drivers": {
        "bacnet_ip": {"interface": "127.0.0.1", "port": 0, "broadcast": None, "cov": False, "discover_targets": ["a"]},
        "mqtt": {"tls": True, "stale_after_s": 60, "keepalive_s": 10},
    }}, tmp_path)
    # An explicit null is kept: the BACnet/IP driver reads it as "no global Who-Is".
    assert config.driver_settings(ProtocolName.BACNET_IP) == {
        "interface": "127.0.0.1", "port": 0, "broadcast": None, "cov": False, "discover_targets": ["a"]}
    assert config.driver_settings(ProtocolName.MQTT) == {"tls": True, "stale_after_s": 60, "keepalive_s": 10}


def test_unknown_keys_and_bad_values_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed) as info:
        HubConfig.from_dict({"listen": {"port": 99999}, "drivers": {"bacnet_uc": {"timout_s": 1}},
                             "llm": {"provider": "openai"}, "agent": {"result_inline_limit": 10}}, tmp_path)
    text = "\n".join(info.value.errors)
    for where in ("listen.port", "drivers.bacnet_uc.timout_s", "llm.provider", "agent.result_inline_limit"):
        assert where in text
    with pytest.raises(ValidationFailed) as info:
        HubConfig.from_dict({"base_dir": "/"}, tmp_path)
    assert info.value.errors[0].startswith("base_dir: ")
    with pytest.raises(ValidationFailed) as info:
        HubConfig.from_dict({"drivers": {"mqtt": {"password": "x", "password_env": "Y"}}}, tmp_path)
    assert "not both" in info.value.errors[0]
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a list\n")
    with pytest.raises(ValidationFailed, match="mapping"):
        load_config(bad)
    with pytest.raises(ValidationFailed, match="cannot read"):
        load_config(tmp_path / "missing.yaml")


def test_secrets_never_show(tmp_path: Path) -> None:
    config = HubConfig.from_dict({
        "auth": {"tokens": [{"user": "tech1", "roles": ["operator"], "token": "s3cret-token"}]},
        "llm": {"provider": "zai", "api_key": "zai-key-123"},
        "drivers": {"mqtt": {"username": "hub", "password": "mqtt-pw"}},
    }, tmp_path)
    for secret in ("s3cret-token", "zai-key-123", "mqtt-pw"):
        assert secret not in repr(config)
        assert secret not in str(config.model_dump())
    assert config.driver_settings(ProtocolName.MQTT)["password"] == "mqtt-pw"
    assert config.llm_settings()["api_key"] == "zai-key-123"
    with pytest.raises(ValidationFailed) as info:
        HubConfig.from_dict({"auth": {"tokens": [{"user": "x", "roles": ["root"], "token": "s3cret-token"}]}},
                            tmp_path)
    assert "s3cret-token" not in str(info.value) + "".join(info.value.errors)


def test_identities_from_literals_and_environment(tmp_path: Path) -> None:
    auth = HubConfig.from_dict({"auth": {"tokens": [
        {"user": "ilija", "roles": ["admin"], "token_env": "UC_HUB_TOKEN_ILIJA"},
        {"user": "tech1", "roles": ["operator", "viewer"], "token": " literal "},
    ]}}, tmp_path).auth
    assert not auth.dev_mode
    assert auth.identities({"UC_HUB_TOKEN_ILIJA": "t-ilija"}) == {
        "t-ilija": Identity("ilija", frozenset({"admin"})),
        "literal": Identity("tech1", frozenset({"operator", "viewer"})),
    }
    with pytest.raises(ValidationFailed) as info:
        auth.identities({})
    assert info.value.errors == [
        "auth token of 'ilija': the environment variable UC_HUB_TOKEN_ILIJA is empty or not set"]
    with pytest.raises(ValidationFailed) as info:
        auth.identities({"UC_HUB_TOKEN_ILIJA": "literal"})
    assert info.value.errors == ["auth token of 'tech1' is also the token of 'ilija'"]
    with pytest.raises(ValidationFailed) as info:
        HubConfig.from_dict({"auth": {"tokens": [{"user": "x", "roles": ["admin"]}]}}, tmp_path)
    assert "exactly one of token and token_env" in info.value.errors[0]


def test_dev_mode_only_listens_on_loopback(tmp_path: Path) -> None:
    """Without tokens every request is user "dev" with every role, so such a
    hub must not be reachable from the network."""
    for host in ("0.0.0.0", "::", "10.0.2.5", "hub.local", ""):
        with pytest.raises(ValidationFailed) as info:
            HubConfig.from_dict({"listen": {"host": host}}, tmp_path)
        assert info.value.errors == ["listen.host: without auth.tokens (dev mode) the hub listens on a loopback "
                                     "address only (127.0.0.1, ::1 or localhost)"], host
    for host in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
        assert HubConfig.from_dict({"listen": {"host": host}}, tmp_path).auth.dev_mode
    public = HubConfig.from_dict({"listen": {"host": "0.0.0.0"}, "auth": {"tokens": [
        {"user": "tech1", "roles": ["operator"], "token_env": "UC_HUB_TOKEN_TECH1"}]}}, tmp_path)
    assert not public.auth.dev_mode and public.listen.host == "0.0.0.0"
    # --insecure-listen, deliberately.
    path = write(tmp_path, {"listen": {"host": "0.0.0.0"}})
    assert load_config(path, insecure_listen=True).auth.dev_mode
    with pytest.raises(ValidationFailed):
        load_config(path)


async def test_llm_script_is_relative_to_hub_yaml(tmp_path: Path) -> None:
    path = write(tmp_path, {"llm": {"provider": "scripted", "script": "scripts/test.yaml", "delay_s": 0}})
    (path.parent / "scripts").mkdir()
    (path.parent / "scripts" / "test.yaml").write_text("model: from-file\nrules: []\n")
    config = load_config(path)
    provider = make_provider(config.llm_settings(), base_dir=config.base_dir)
    assert isinstance(provider, ScriptedProvider) and provider.model == "from-file"
    assert isinstance(make_provider(load_config(write(tmp_path, {})).llm_settings()), DisabledProvider)

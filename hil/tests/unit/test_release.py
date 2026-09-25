"""DutImage and the SEC-01 release guard (hilrig.release) on synthetic build directories."""

from __future__ import annotations

from pathlib import Path

import pytest

from hilrig.release import DutImage, check_release, config_diff, keylog_strings, read_config

BACNET = """\
CONFIG_BOARD="nucleo_f767zi"
CONFIG_BOARD_TARGET="nucleo_f767zi"
CONFIG_UC_FW_VERSION="0.1.0"
CONFIG_UC_APP_POOL_SIZE=131072
# CONFIG_HIL is not set
CONFIG_NET_DHCPV4=y
"""
MQTT = """\
CONFIG_BOARD="nucleo_f767zi"
CONFIG_BOARD_TARGET="nucleo_f767zi"
CONFIG_APP_MQTT_BROKER_HOSTNAME="test.mosquitto.org"
CONFIG_APP_MQTT_TLS_CA_CERT_FILE="certs/mosquitto.org.crt"
CONFIG_APP_MQTT_PUBLISH_INTERVAL_SEC=10
CONFIG_APP_MQTT_CLIENT_ID=""
# CONFIG_APP_MQTT_TLS_CLIENT_AUTH is not set
"""


def build(
    tmp_path: Path, name: str, config: str, elf: bytes | None = b"\x7fELF", version: str | None = None
) -> Path:
    root = tmp_path / name
    (root / "zephyr").mkdir(parents=True)
    (root / "zephyr" / ".config").write_text(config)
    (root / "CMakeCache.txt").write_text(f"APPLICATION_SOURCE_DIR:PATH=/src/{name}\n")
    if elf is not None:
        (root / "zephyr" / "zephyr.elf").write_bytes(elf)
    if version:
        header = root / "zephyr" / "include" / "generated" / "zephyr"
        header.mkdir(parents=True)
        (header / "app_version.h").write_text(f'#define APP_VERSION_STRING "{version}"\n')
    return root


def edit(config: str, **symbols: str) -> str:
    lines = [line for line in config.splitlines() if not any(f"CONFIG_{k}" in line for k in symbols)]
    return "\n".join(lines + [f"CONFIG_{k}={v}" for k, v in symbols.items()]) + "\n"


def test_read_config_keeps_raw_values_and_unset_symbols(tmp_path: Path) -> None:
    path = tmp_path / ".config"
    path.write_text(BACNET)
    config = read_config(path)
    assert config["CONFIG_UC_FW_VERSION"] == '"0.1.0"' and config["CONFIG_HIL"] == "n"


def test_image_kinds(tmp_path: Path) -> None:
    bacnet = DutImage.from_build(build(tmp_path, "bac", BACNET))
    assert (bacnet.app, bacnet.kind, bacnet.board, bacnet.fw_version, bacnet.instrumented) == (
        "bacnet",
        "bacnet",
        "nucleo_f767zi",
        "0.1.0",
        False,
    )
    assert bacnet.source_dir == "/src/bac" and not bacnet.native
    mqtt = DutImage.from_build(build(tmp_path, "mq", MQTT, version="0.3.0"))
    assert (mqtt.app, mqtt.kind, mqtt.fw_version, mqtt.publish_interval_s, mqtt.client_id) == (
        "mqtt",
        "mqtt",
        "0.3.0",
        10,
        None,
    )
    mtls = DutImage.from_build(build(tmp_path, "mtls", edit(MQTT, APP_MQTT_TLS_CLIENT_AUTH="y")))
    assert mtls.kind == "mqtt-mtls" and mtls.variant is None
    variant = DutImage.from_build(build(tmp_path, "v", edit(MQTT, APP_MQTT_PUBLISH_INTERVAL_SEC="120")))
    assert variant.variant == "publish120" and variant.publish_interval_s == 120
    instrumented = DutImage.from_build(build(tmp_path, "i", edit(BACNET, HIL="y")))
    assert instrumented.instrumented
    with pytest.raises(FileNotFoundError, match="not a Zephyr build"):
        DutImage.from_build(tmp_path / "nothing")
    with pytest.raises(ValueError, match="neither"):
        DutImage.from_build(build(tmp_path, "other", 'CONFIG_BOARD="x"\n'))


def test_sec01_allows_exactly_the_d24_symbols(tmp_path: Path) -> None:
    plain = DutImage.from_build(build(tmp_path, "plain", MQTT))
    site = edit(
        MQTT, APP_MQTT_BROKER_HOSTNAME='"broker.hil.lan"', APP_MQTT_TLS_CA_CERT_FILE='"/hil/pki/ca.crt"'
    )
    report = check_release(DutImage.from_build(build(tmp_path, "rel", site)), plain)
    assert report.ok and set(report.allowed) == {
        "CONFIG_APP_MQTT_BROKER_HOSTNAME",
        "CONFIG_APP_MQTT_TLS_CA_CERT_FILE",
    }
    mtls = edit(
        site,
        APP_MQTT_TLS_CLIENT_AUTH="y",
        APP_MQTT_TLS_CLIENT_CERT_FILE='"/hil/pki/dut-client.crt"',
        APP_MQTT_TLS_CLIENT_KEY_FILE='"/hil/pki/dut-client.key"',
    )
    assert check_release(DutImage.from_build(build(tmp_path, "mtls", mtls)), plain).ok
    sneaky = edit(site, NET_SHELL="y")
    bad = check_release(DutImage.from_build(build(tmp_path, "sneaky", sneaky)), plain)
    assert not bad.ok and bad.unexpected == {"CONFIG_NET_SHELL": ("y", None)}
    mtls_symbol_on_plain_kind = edit(site, APP_MQTT_TLS_CLIENT_CERT_FILE='"x"')
    assert not check_release(
        DutImage.from_build(build(tmp_path, "half", mtls_symbol_on_plain_kind)), plain
    ).ok


def test_sec01_variant_adds_exactly_one_symbol(tmp_path: Path) -> None:
    plain = DutImage.from_build(build(tmp_path, "plain", MQTT))
    site = edit(MQTT, APP_MQTT_BROKER_HOSTNAME='"broker.hil.lan"', APP_MQTT_PUBLISH_INTERVAL_SEC="120")
    report = check_release(DutImage.from_build(build(tmp_path, "var", site)), plain)
    assert report.ok and report.variant == "publish120"
    other = check_release(DutImage.from_build(build(tmp_path, "var2", edit(site, MQTT_KEEPALIVE="5"))), plain)
    assert not other.ok and list(other.unexpected) == ["CONFIG_MQTT_KEEPALIVE"]


def test_sec01_hil_symbols_keylog_and_missing_elf(tmp_path: Path) -> None:
    plain = DutImage.from_build(build(tmp_path, "plain", BACNET))
    pool = edit(BACNET, UC_APP_POOL_SIZE="98304")
    assert check_release(DutImage.from_build(build(tmp_path, "rel", pool)), plain).ok
    hil = check_release(
        DutImage.from_build(build(tmp_path, "hil", edit(pool, HIL="y", HIL_SHELL="y"))), plain
    )
    assert hil.hil_symbols == ["CONFIG_HIL", "CONFIG_HIL_SHELL"] and not hil.ok
    elf = b"\x7fELF\x00\x01KEYLOG CLIENT_RANDOM %s\x00junk\x00KEYLOG x\x00"
    keylog = check_release(DutImage.from_build(build(tmp_path, "kl", pool, elf=elf)), plain)
    assert keylog.keylog_strings == 2 and not keylog.ok
    unlinked = check_release(DutImage.from_build(build(tmp_path, "cmake-only", pool, elf=None)), plain)
    assert unlinked.keylog_strings is None and not unlinked.ok
    with pytest.raises(ValueError, match="plain build is"):
        check_release(DutImage.from_build(build(tmp_path, "mq", MQTT)), plain)


def test_keylog_strings_counts_like_strings_grep(tmp_path: Path) -> None:
    elf = tmp_path / "zephyr.elf"
    elf.write_bytes(b"KEY\x00LOG \x00abcKEYLOG def\x00\x01KEYLOG\x00")
    assert keylog_strings(elf) == 1  # only the complete marker with its space counts
    assert config_diff({"A": "1", "B": "2"}, {"A": "1", "C": "3"}) == {"B": ("2", None), "C": (None, "3")}

"""What a DUT build is, and the release-artifact guard SEC-01 (D24).

:class:`DutImage` reads a Zephyr build directory (``zephyr/.config``, ``CMakeCache.txt``,
``app_version.h``) and says which app it is (BACnet ``firmware/`` or ``apps/mqtt_tls``),
whether it is instrumented (``-S hil``: ``CONFIG_HIL=y``), mutual TLS, or a variant, so tests
pick their criteria per image (HIL.md 1: the two apps are separate images).

SEC-01 checks a release build statically, in the build job (packaging drops ``.config``):

- no ``CONFIG_HIL*`` symbol is set;
- ``strings zephyr.elf | grep -c 'KEYLOG '`` is 0 (the It2 key-log shim's marker);
- the ``.config`` differs from the plain app build (same sources and board, no site
  configuration) only by the D24 allowlist of its artifact kind; a variant image adds exactly
  one documented symbol and is reported separately.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

App = Literal["bacnet", "mqtt", "standin"]

# D24 site-configuration allowlist per artifact kind (symbols that may differ from the plain
# app build). BACnet plain: the app-pool workaround until FW-04 (its devicetree overlay moves
# the pool to DTCM and changes no Kconfig symbol).
ALLOWLIST: Mapping[str, frozenset[str]] = {
    "bacnet": frozenset({"CONFIG_UC_APP_POOL_SIZE"}),
    "mqtt": frozenset({"CONFIG_APP_MQTT_BROKER_HOSTNAME", "CONFIG_APP_MQTT_TLS_CA_CERT_FILE"}),
    "mqtt-mtls": frozenset(
        {
            "CONFIG_APP_MQTT_BROKER_HOSTNAME",
            "CONFIG_APP_MQTT_TLS_CA_CERT_FILE",
            "CONFIG_APP_MQTT_TLS_CLIENT_AUTH",
            "CONFIG_APP_MQTT_TLS_CLIENT_CERT_FILE",
            "CONFIG_APP_MQTT_TLS_CLIENT_KEY_FILE",
        }
    ),
}
# Variant images: release plus exactly one documented test symbol (D24).
VARIANTS: Mapping[str, tuple[str, str]] = {
    "publish120": ("CONFIG_APP_MQTT_PUBLISH_INTERVAL_SEC", "120"),  # MQTT-09
}
KEYLOG_MARKER = b"KEYLOG "

_CONFIG_LINE = re.compile(r"^(CONFIG_\w+)=(.*)$")
_UNSET_LINE = re.compile(r"^# (CONFIG_\w+) is not set$")
_PRINTABLE = re.compile(rb"[\t\x20-\x7e]{4,}")  # what strings(1) prints by default


def read_config(path: Path) -> dict[str, str]:
    """Parse a Kconfig ``.config``: set symbols with their raw value, unset ones as ``n``."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if m := _CONFIG_LINE.match(line):
            values[m[1]] = m[2]
        elif m := _UNSET_LINE.match(line):
            values[m[1]] = "n"
    return values


def unquote(value: str | None) -> str | None:
    """A Kconfig string value without its quotes."""
    if value is not None and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _cmake_cache(build: Path) -> dict[str, str]:
    path = build / "CMakeCache.txt"
    if not path.exists():
        return {}
    cache = {}
    for line in path.read_text(errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.startswith(("#", "//")):
            cache[key.partition(":")[0]] = value
    return cache


@dataclass(frozen=True)
class DutImage:
    """A DUT firmware image: which app, which board, and its build configuration."""

    app: App
    build_dir: Path | None = None
    config: Mapping[str, str] = field(default_factory=dict)
    source_dir: str | None = None
    app_version: str | None = None

    @classmethod
    def from_build(cls, build_dir: Path) -> DutImage:
        """Read ``build_dir`` (the Zephyr build of the image, or its sysbuild app domain)."""
        config_path = build_dir / "zephyr" / ".config"
        if not config_path.exists():
            raise FileNotFoundError(f"{config_path}: not a Zephyr build directory")
        config = read_config(config_path)
        if "CONFIG_UC_FW_VERSION" in config:
            app: App = "bacnet"
        elif "CONFIG_APP_MQTT_BROKER_HOSTNAME" in config:
            app = "mqtt"
        else:
            raise ValueError(f"{build_dir}: neither the BACnet firmware nor apps/mqtt_tls")
        version_h = build_dir / "zephyr" / "include" / "generated" / "zephyr" / "app_version.h"
        version = None
        if version_h.exists() and (
            m := re.search(r'#define APP_VERSION_STRING "([^"]*)"', version_h.read_text())
        ):
            version = m[1]
        source = _cmake_cache(build_dir).get("APPLICATION_SOURCE_DIR")
        return cls(app, build_dir, config, source, version)

    def value(self, symbol: str) -> str | None:
        """A symbol's value without string quotes (None if unset or absent)."""
        raw = self.config.get(symbol)
        return None if raw in (None, "n") else unquote(raw)

    @property
    def board(self) -> str | None:
        """The board target (CONFIG_BOARD_TARGET), e.g. nucleo_f767zi."""
        return self.value("CONFIG_BOARD_TARGET") or self.value("CONFIG_BOARD")

    @property
    def native(self) -> bool:
        """A native_sim executable (SIL)."""
        return (self.board or "").startswith("native_sim")

    @property
    def instrumented(self) -> bool:
        """Built with ``-S hil`` (lib/hil: HIL-BOOT/HIL-READY, markers, hil shell)."""
        return self.value("CONFIG_HIL") == "y"

    @property
    def mtls(self) -> bool:
        """An MQTT image that authenticates with a client certificate."""
        return self.value("CONFIG_APP_MQTT_TLS_CLIENT_AUTH") == "y"

    @property
    def fw_version(self) -> str | None:
        """BACnet: CONFIG_UC_FW_VERSION (Firmware_Revision); MQTT: the app VERSION (info fw)."""
        return self.value("CONFIG_UC_FW_VERSION") if self.app == "bacnet" else self.app_version

    @property
    def publish_interval_s(self) -> int:
        """MQTT telemetry interval (Kconfig default 10 s)."""
        return int(self.value("CONFIG_APP_MQTT_PUBLISH_INTERVAL_SEC") or 10)

    @property
    def client_id(self) -> str | None:
        """A configured MQTT client id (None: derived from the UID, FW-02)."""
        return self.value("CONFIG_APP_MQTT_CLIENT_ID") or None

    @property
    def variant(self) -> str | None:
        """The D24 variant this image is (publish120), or None for a release image."""
        for name, (symbol, value) in VARIANTS.items():
            if self.value(symbol) == value:
                return name
        return None

    @property
    def kind(self) -> str:
        """The D24 artifact kind: bacnet, mqtt or mqtt-mtls."""
        return "mqtt-mtls" if self.app == "mqtt" and self.mtls else self.app


@dataclass
class Sec01Report:
    """SEC-01 findings for one release build (empty lists: pass)."""

    build: Path
    kind: str
    variant: str | None
    hil_symbols: list[str]
    keylog_strings: int | None  # None: the build has no zephyr.elf (not linked)
    unexpected: dict[str, tuple[str | None, str | None]]
    allowed: dict[str, tuple[str | None, str | None]]

    @property
    def ok(self) -> bool:
        return not self.hil_symbols and self.keylog_strings == 0 and not self.unexpected


def keylog_strings(elf: Path) -> int:
    """``strings <elf> | grep -c 'KEYLOG '``, computed in Python (no binutils needed)."""
    return sum(1 for s in _PRINTABLE.findall(elf.read_bytes()) if KEYLOG_MARKER in s)


def config_diff(
    release: Mapping[str, str], plain: Mapping[str, str]
) -> dict[str, tuple[str | None, str | None]]:
    """Symbols whose value differs: {symbol: (release value, plain value)}; absent = None."""
    names = set(release) | set(plain)
    return {n: (release.get(n), plain.get(n)) for n in sorted(names) if release.get(n) != plain.get(n)}


def check_release(release: DutImage, plain: DutImage) -> Sec01Report:
    """Run SEC-01 on a release build against the plain build of the same app and board."""
    if release.build_dir is None:
        raise ValueError("the release image has no build directory")
    if (release.app, release.board) != (plain.app, plain.board):
        raise ValueError(
            f"plain build is {plain.app}/{plain.board}, release is {release.app}/{release.board}"
        )
    hil = sorted(k for k, v in release.config.items() if k.startswith("CONFIG_HIL") and v != "n")
    elf = release.build_dir / "zephyr" / "zephyr.elf"
    diff = config_diff(release.config, plain.config)
    allowed_names = set(ALLOWLIST[release.kind])
    variant = release.variant
    if variant is not None:
        allowed_names.add(VARIANTS[variant][0])
    allowed = {k: v for k, v in diff.items() if k in allowed_names}
    unexpected = {k: v for k, v in diff.items() if k not in allowed_names}
    return Sec01Report(
        release.build_dir,
        release.kind,
        variant,
        hil,
        keylog_strings(elf) if elf.exists() else None,
        unexpected,
        allowed,
    )

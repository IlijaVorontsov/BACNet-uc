#!/usr/bin/env python3
"""Render the Twister alt configs of a HIL run (docs/HIL.md 8.5, D28, D31).

The firmware branches keep their own sample.yaml files (build-only scenarios). A HIL run
replaces them with ``--alt-config-root``: Twister looks for
``<alt root>/relpath(<suite dir>, <-T root>)/sample.yaml``, and every run uses the suite dir
itself as ``-T``, so each app gets its own alt root with the file at its top:

    OUT/alt/bacnet/sample.yaml   -T BACNET/firmware      hil.bacnet.release, .instrumented
                                                          (It2: hil.bacnet.release.mcuboot)
    OUT/alt/mqtt/sample.yaml     -T MQTT/apps/mqtt_tls   hil.mqtt.release, .release.mtls,
                                                          .instrumented (It2: hil.mqtt.variant)

Each scenario is a pytest scenario on the bench DUT: ``harness: pytest``, ``tags: [hil]``,
``timeout: 3600`` (the Twister default of 60 s kills pytest), the ``hil_bench`` fixture (the
DUT in map.yml carries ``hil_bench:<bench.yml>``), ``pytest_root: [$HIL_TESTS]`` (expanded by
Twister when pytest starts; run-hil sets it) and session DUT scope. The images are those of
``hil/host/build.sh`` (D24): the same site configuration, the firmware checkout as the
``bacnet-uc`` module, and for instrumented images the HIL snippets and ``lib/hil``.

Twister expands no variables in ``extra_args`` and the MQTT site files name the TEST-ONLY PKI
as ``@HIL@/...``, so everything is rendered with absolute paths: the site files into
``OUT/site/``, and the paths inside the scenarios. Paths may not contain whitespace, ``;``
(CMake list separator), ``"`` (Twister strips quotes) or newlines.

D28: one Twister invocation per app, each with its own alt root and ``-O OUT/<app>``, identical
for ``--build-only`` and ``--test-only``. ``OUT/twister/<app>.args`` holds those common
arguments one per line; hil/twister/ci-build.sh and run-hil both read that file, so the two
stages cannot drift apart. Each scenario writes its pytest artifacts (pcaps, stim.log, service
logs, rig reports) to ``OUT/results/<app>/<scenario>`` with ``--artifacts``.

The common arguments include ``--disable-warnings-as-errors``. By default Twister builds with
``CONFIG_COMPILER_WARNINGS_AS_ERRORS=y`` and ``--edtlib-Werror``: the first puts a symbol into
the release ``.config`` that the plain app build does not have (SEC-01 allows only the D24
site symbols), the second rejects the BACnet catalog binding (``uc,io-channels``: vendor
prefix ``uc`` is not in Zephyr's list). With it the Twister images equal build.sh's.

Usage: gen.py --out OUT --hil HIL --bacnet FW --mqtt MQ [--hardware-map MAP] [--platform P] [--it2]
Exit status: 0 rendered, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

App = Literal["bacnet", "mqtt"]
Tier = Literal["release", "instrumented", "variant"]

APPS: tuple[App, ...] = ("bacnet", "mqtt")
SUITE_DIRS: dict[App, str] = {"bacnet": "firmware", "mqtt": "apps/mqtt_tls"}  # -T per app
SUITE_FILE = "sample.yaml"  # the name both suites use, so the alt file has it too
TAG = "hil"
FIXTURE = "hil_bench"
TIMEOUT_S = 3600
PYTEST_ROOT = "$HIL_TESTS"
DEFAULT_MAP = "/etc/hil/bench1/map.yml"
DEFAULT_PLATFORM = "nucleo_f767zi"
APP_POOL_SIZE = 98304  # FW-04 workaround, with site/f767-app-pool.overlay
PYTEST_ARGS: dict[Tier, tuple[str, ...]] = {
    "release": ("--enable-slow", "-m", "rig or release"),
    "instrumented": ("--enable-slow", "-m", "rig or instrumented"),
    "variant": ("-m", "rig or variant"),
}
_BAD_PATH = re.compile(r'[\s;"]')


class GenError(Exception):
    """An input is missing or unusable (exit status 2)."""


@dataclass(frozen=True)
class Paths:
    """The absolute inputs and output of one rendering."""

    out: Path
    hil: Path
    bacnet: Path
    mqtt: Path
    hardware_map: Path
    platform: str = DEFAULT_PLATFORM

    def suite(self, app: App) -> Path:
        """The app's testsuite dir, used as Twister's ``-T``."""
        return (self.bacnet if app == "bacnet" else self.mqtt) / SUITE_DIRS[app]

    def alt_root(self, app: App) -> Path:
        return self.out / "alt" / app

    def outdir(self, app: App) -> Path:
        return self.out / app

    def artifacts(self, app: App, scenario: str) -> Path:
        return self.out / "results" / app / scenario


@dataclass(frozen=True)
class Scenario:
    """One HIL Twister scenario: the image it builds and the tier of tests it runs."""

    name: str
    app: App
    tier: Tier
    extra_args: tuple[str, ...]
    snippets: tuple[str, ...] = ()
    sysbuild: bool = False


def scenarios(p: Paths, *, it2: bool = False) -> list[Scenario]:
    """The scenarios of one run, as ``hil/host/build.sh`` builds the same images."""
    site = p.out / "site"
    pool = (
        f"EXTRA_DTC_OVERLAY_FILE={p.hil}/hil/site/f767-app-pool.overlay",
        f"CONFIG_UC_APP_POOL_SIZE={APP_POOL_SIZE}",
    )
    instrumented = f"{p.hil}/lib/hil"
    snippet_root = f"SNIPPET_ROOT={p.hil}"
    plain = f"{site}/mqtt-site-hil.conf"
    result = [
        Scenario("hil.bacnet.release", "bacnet", "release", (*pool, f"ZEPHYR_EXTRA_MODULES={p.bacnet}")),
        Scenario(
            "hil.bacnet.instrumented",
            "bacnet",
            "instrumented",
            (*pool, f"ZEPHYR_EXTRA_MODULES={p.bacnet};{instrumented}", snippet_root),
            snippets=("hil", "hil-io"),
        ),
        Scenario(
            "hil.mqtt.release",
            "mqtt",
            "release",
            (f"ZEPHYR_EXTRA_MODULES={p.mqtt}", f"EXTRA_CONF_FILE={plain}"),
        ),
        Scenario(
            "hil.mqtt.release.mtls",
            "mqtt",
            "release",
            (f"ZEPHYR_EXTRA_MODULES={p.mqtt}", f"EXTRA_CONF_FILE={plain};{site}/mqtt-site-hil-mtls.conf"),
        ),
        Scenario(
            "hil.mqtt.instrumented",
            "mqtt",
            "instrumented",
            (f"ZEPHYR_EXTRA_MODULES={p.mqtt};{instrumented}", f"EXTRA_CONF_FILE={plain}", snippet_root),
            snippets=("hil",),
        ),
    ]
    if it2:
        result += [
            # OTA-* only: the firmware's own MCUboot sysbuild configuration plus the workaround
            Scenario(
                "hil.bacnet.release.mcuboot",
                "bacnet",
                "release",
                (*pool, f"ZEPHYR_EXTRA_MODULES={p.bacnet}", "EXTRA_CONF_FILE=overlay-mcuboot.conf"),
                sysbuild=True,
            ),
            # MQTT-09: the release image plus exactly one documented test symbol
            Scenario(
                "hil.mqtt.variant",
                "mqtt",
                "variant",
                (f"ZEPHYR_EXTRA_MODULES={p.mqtt}", f"EXTRA_CONF_FILE={plain};{site}/variant-publish120.conf"),
            ),
        ]
    return result


def scenario_yaml(s: Scenario, p: Paths) -> dict[str, Any]:
    """The ``tests:`` entry of one scenario (every key explicit: no ``common:`` merging)."""
    entry: dict[str, Any] = {
        "harness": "pytest",
        "tags": [TAG],
        "timeout": TIMEOUT_S,
        "platform_allow": [p.platform],
        "integration_platforms": [p.platform],
    }
    if s.sysbuild:
        entry["sysbuild"] = True
    if s.snippets:
        entry["required_snippets"] = list(s.snippets)
    entry["extra_args"] = list(s.extra_args)
    entry["harness_config"] = {
        "fixture": FIXTURE,
        "pytest_root": [PYTEST_ROOT],
        "pytest_dut_scope": "session",
        "pytest_args": [*PYTEST_ARGS[s.tier], f"--artifacts={p.artifacts(s.app, s.name)}"],
    }
    return entry


def sample_yaml(app: App, items: Sequence[Scenario], p: Paths) -> str:
    """The alt ``sample.yaml`` of one app, with a header that says where it came from."""
    data = {
        "sample": {
            "name": f"HIL {app}",
            "description": f"HIL rig scenarios for {p.suite(app)} (rendered by hil/twister/gen.py)",
        },
        "tests": {s.name: scenario_yaml(s, p) for s in items if s.app == app},
    }
    header = (
        "# Rendered by hil/twister/gen.py (docs/HIL.md 8.5, D28). Do not edit: re-run gen.py.\n"
        f"# west twister -T {p.suite(app)} --alt-config-root {p.alt_root(app)} -O {p.outdir(app)} ...\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=1000)


def twister_args(app: App, p: Paths) -> list[str]:
    """The arguments shared by the ``--build-only`` and ``--test-only`` calls of one app (D28)."""
    return [
        "-T",
        str(p.suite(app)),
        "--alt-config-root",
        str(p.alt_root(app)),
        "-p",
        p.platform,
        "--tag",
        TAG,
        "--device-testing",
        "--hardware-map",
        str(p.hardware_map),
        "-O",
        str(p.outdir(app)),
        "--disable-warnings-as-errors",
        "--allow-installed-plugin",
    ]


def render_site(hil: Path, out: Path) -> list[Path]:
    """Copy ``hil/site/*.conf`` into ``out/site`` with ``@HIL@`` = the HIL checkout (as build.sh)."""
    site = out / "site"
    site.mkdir(parents=True, exist_ok=True)
    written = []
    for src in sorted((hil / "hil" / "site").glob("*.conf")):
        dst = site / src.name
        dst.write_text(src.read_text().replace("@HIL@", str(hil)))
        written.append(dst)
    return written


def check_inputs(p: Paths, items: Sequence[Scenario]) -> None:
    """Fail early, with the path, when an input is missing or cannot be passed to Twister."""
    for path in (p.out, p.hil, p.bacnet, p.mqtt, p.hardware_map):
        if not path.is_absolute() or _BAD_PATH.search(str(path)):
            raise GenError(f"{path}: must be absolute, without whitespace, ';' or '\"'")
    required = [
        p.suite("bacnet") / SUITE_FILE,
        p.suite("mqtt") / SUITE_FILE,
        p.hil / "hil" / "site" / "f767-app-pool.overlay",
        p.hil / "hil" / "site" / "mqtt-site-hil.conf",
    ]
    if any(s.tier == "instrumented" for s in items):
        required.append(p.hil / "lib" / "hil" / "zephyr" / "module.yml")
    required += [p.hil / "snippets" / name / "snippet.yml" for s in items for name in s.snippets]
    missing = [str(path) for path in dict.fromkeys(required) if not path.is_file()]
    if missing:
        raise GenError(f"missing: {', '.join(missing)}")
    if not re.fullmatch(r"[a-z0-9_]+(/[a-z0-9_]+)*", p.platform):
        raise GenError(f"--platform {p.platform!r} is not a board target")


def render(p: Paths, *, it2: bool = False) -> dict[App, list[str]]:
    """Write the site files, both alt configs and both args files; return the scenario names."""
    items = scenarios(p, it2=it2)
    check_inputs(p, items)
    render_site(p.hil, p.out)
    (p.out / "twister").mkdir(parents=True, exist_ok=True)
    names: dict[App, list[str]] = {}
    for app in APPS:
        alt = p.alt_root(app)
        alt.mkdir(parents=True, exist_ok=True)
        (alt / SUITE_FILE).write_text(sample_yaml(app, items, p))
        (p.out / "twister" / f"{app}.args").write_text("\n".join(twister_args(app, p)) + "\n")
        names[app] = [s.name for s in items if s.app == app]
    return names


def parse_args(argv: Sequence[str] | None) -> tuple[Paths, bool]:
    parser = argparse.ArgumentParser(
        description="Render the Twister alt configs of a HIL run (docs/HIL.md 8.5)."
    )
    parser.add_argument("--out", required=True, type=Path, help="run directory (/opt/hil/out/<run>)")
    parser.add_argument("--hil", required=True, type=Path, help="HIL checkout (snapshot)")
    parser.add_argument("--bacnet", required=True, type=Path, help="BACnet firmware checkout")
    parser.add_argument("--mqtt", required=True, type=Path, help="MQTT (apps/mqtt_tls) checkout")
    parser.add_argument("--hardware-map", type=Path, default=Path(DEFAULT_MAP), help=f"default {DEFAULT_MAP}")
    parser.add_argument("--platform", default=DEFAULT_PLATFORM, help=f"default {DEFAULT_PLATFORM}")
    parser.add_argument("--it2", action="store_true", help="add the It2 scenarios (MCUboot, variant)")
    a = parser.parse_args(argv)
    paths = Paths(
        out=a.out.absolute(),
        hil=a.hil.resolve(),
        bacnet=a.bacnet.resolve(),
        mqtt=a.mqtt.resolve(),
        hardware_map=a.hardware_map.absolute(),
        platform=a.platform,
    )
    return paths, a.it2


def main(argv: Sequence[str] | None = None) -> int:
    paths, it2 = parse_args(argv)
    try:
        names = render(paths, it2=it2)
    except (GenError, OSError) as e:
        print(f"gen.py: {e}", file=sys.stderr)
        return 2
    for app, scenario_names in names.items():
        print(f"{paths.alt_root(app) / SUITE_FILE}: {' '.join(scenario_names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

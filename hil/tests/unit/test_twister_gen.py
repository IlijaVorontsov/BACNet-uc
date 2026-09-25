"""hil/twister/gen.py: the Twister alt configs of a HIL run (HIL.md 8.5, D24, D28, D31).

The renderer runs on a synthetic tree (fake firmware checkouts) that carries the real
``hil/site`` files, so the ``@HIL@`` rendering is checked on what the builds use. When a
Zephyr tree is found (``$ZEPHYR_BASE``, or ``zephyr/`` next to this checkout in its west
workspace), the rendered files are also parsed by Twister's own schema and scenario loader.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

HIL_ROOT = Path(__file__).resolve().parents[2]  # hil/
GEN = HIL_ROOT / "twister" / "gen.py"
IT1 = {
    "bacnet": ["hil.bacnet.release", "hil.bacnet.instrumented"],
    "mqtt": ["hil.mqtt.release", "hil.mqtt.release.mtls", "hil.mqtt.instrumented"],
}
IT2 = {"bacnet": ["hil.bacnet.release.mcuboot"], "mqtt": ["hil.mqtt.variant"]}
FLAGS = {"--device-testing", "--disable-warnings-as-errors", "--allow-installed-plugin"}


def _load_gen() -> Any:
    spec = importlib.util.spec_from_file_location("hil_twister_gen", GEN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


gen = _load_gen()


@pytest.fixture
def paths(tmp_path: Path) -> Any:
    """Paths of a synthetic run: HIL checkout with the real site files, two fake firmware trees."""
    hil, fw, mq = tmp_path / "hil", tmp_path / "fw", tmp_path / "mq"
    shutil.copytree(HIL_ROOT / "site", hil / "hil" / "site", ignore=shutil.ignore_patterns("sil"))
    for rel in ("lib/hil/zephyr/module.yml", "snippets/hil/snippet.yml", "snippets/hil-io/snippet.yml"):
        (hil / rel).parent.mkdir(parents=True, exist_ok=True)
        (hil / rel).write_text("name: x\n")
    for suite in (fw / "firmware", mq / "apps" / "mqtt_tls"):
        suite.mkdir(parents=True)
        (suite / "sample.yaml").write_text("tests:\n  app.x:\n    build_only: true\n")
    return gen.Paths(out=tmp_path / "out", hil=hil, bacnet=fw, mqtt=mq, hardware_map=tmp_path / "map.yml")


def rendered(paths: Any, app: str) -> dict[str, Any]:
    tests: dict[str, Any] = yaml.safe_load((paths.alt_root(app) / "sample.yaml").read_text())["tests"]
    return tests


def test_scenarios_per_app_and_iteration(paths: Any) -> None:
    assert gen.render(paths) == IT1
    assert gen.render(paths, it2=True) == {app: IT1[app] + IT2[app] for app in IT1}
    assert list(rendered(paths, "bacnet")) == IT1["bacnet"] + IT2["bacnet"]


def test_every_scenario_is_a_session_scoped_pytest_scenario_on_the_bench(paths: Any) -> None:
    gen.render(paths, it2=True)
    tiers = {"release": "rig or release", "instrumented": "rig or instrumented", "variant": "rig or variant"}
    for app in ("bacnet", "mqtt"):
        for name, entry in rendered(paths, app).items():
            assert (entry["harness"], entry["tags"], entry["timeout"]) == ("pytest", ["hil"], 3600), name
            assert entry["platform_allow"] == ["nucleo_f767zi"]
            config = entry["harness_config"]
            assert config["fixture"] == "hil_bench" and config["pytest_dut_scope"] == "session"
            assert config["pytest_root"] == ["$HIL_TESTS"]  # expanded by Twister when pytest starts
            tier = "variant" if name.endswith("variant") else name.split(".")[2]
            slow = [] if tier == "variant" else ["--enable-slow"]
            assert config["pytest_args"] == [
                *slow,
                "-m",
                tiers[tier],
                f"--artifacts={paths.out}/results/{app}/{name}",
            ]


def test_release_images_carry_site_configuration_only(paths: Any) -> None:
    """D24/SEC-01: no snippet, SNIPPET_ROOT or lib/hil outside the instrumented scenarios."""
    gen.render(paths, it2=True)
    for app in ("bacnet", "mqtt"):
        for name, entry in rendered(paths, app).items():
            args = " ".join(entry["extra_args"])
            module = f"ZEPHYR_EXTRA_MODULES={paths.bacnet if app == 'bacnet' else paths.mqtt}"
            assert module in args, name  # the firmware checkout is the bacnet-uc module
            instrumented = name.endswith(".instrumented")
            assert ("required_snippets" in entry) is instrumented, name
            assert (f"SNIPPET_ROOT={paths.hil}" in entry["extra_args"]) is instrumented, name
            assert (f"{paths.hil}/lib/hil" in args) is instrumented, name
    assert rendered(paths, "bacnet")["hil.bacnet.instrumented"]["required_snippets"] == ["hil", "hil-io"]
    assert rendered(paths, "mqtt")["hil.mqtt.instrumented"]["required_snippets"] == ["hil"]
    assert rendered(paths, "bacnet")["hil.bacnet.release.mcuboot"]["sysbuild"] is True
    assert "CONFIG_UC_APP_POOL_SIZE=98304" in rendered(paths, "bacnet")["hil.bacnet.release"]["extra_args"]


def test_extra_args_name_existing_rendered_files(paths: Any) -> None:
    gen.render(paths, it2=True)
    for app in ("bacnet", "mqtt"):
        for name, entry in rendered(paths, app).items():
            for arg in entry["extra_args"]:
                for value in arg.partition("=")[2].split(";"):
                    if value.startswith("/"):
                        assert Path(value).exists(), f"{name}: {arg}"
    mtls = rendered(paths, "mqtt")["hil.mqtt.release.mtls"]["extra_args"]
    assert (
        f"EXTRA_CONF_FILE={paths.out}/site/mqtt-site-hil.conf;{paths.out}/site/mqtt-site-hil-mtls.conf"
        in mtls
    )
    for conf in (paths.out / "site").glob("*.conf"):
        assert "@HIL@" not in conf.read_text(), conf
    assert f'"{paths.hil}/hil/pki/ca.crt"' in (paths.out / "site" / "mqtt-site-hil.conf").read_text()


def _options(lines: list[str]) -> dict[str, str]:
    """Twister options by name; the flags (FLAGS) map to ''."""
    options: dict[str, str] = {}
    tokens = iter(lines)
    for token in tokens:
        options[token] = "" if token in FLAGS else next(tokens)
    return options


def test_args_files_give_each_app_its_own_alt_root_and_outdir(paths: Any) -> None:
    """D28: build and test read the same file; the two apps share neither alt root nor -O."""
    gen.render(paths)
    options: dict[str, dict[str, str]] = {}
    for app in ("bacnet", "mqtt"):
        lines = (paths.out / "twister" / f"{app}.args").read_text().splitlines()
        assert lines == gen.twister_args(app, paths)
        assert FLAGS | {"--hardware-map"} <= set(lines)
        options[app] = _options(lines)
        root, alt = options[app]["-T"], options[app]["--alt-config-root"]
        assert options[app]["--tag"] == "hil" and options[app]["-O"] == str(paths.out / app)
        # Twister's lookup (testplan.py): <alt root>/relpath(<suite dir>, <-T root>)/sample.yaml
        assert Path(os.path.join(alt, os.path.relpath(root, root), "sample.yaml")).is_file()
    assert options["bacnet"]["-T"] == str(paths.bacnet / "firmware")
    assert options["mqtt"]["-T"] == str(paths.mqtt / "apps" / "mqtt_tls")
    for key in ("--alt-config-root", "-O"):
        assert options["bacnet"][key] != options["mqtt"][key]


def test_render_is_idempotent(paths: Any) -> None:
    gen.render(paths)
    first = {p: p.read_bytes() for p in paths.out.rglob("*") if p.is_file()}
    gen.render(paths)
    assert {p: p.read_bytes() for p in paths.out.rglob("*") if p.is_file()} == first


def test_main_renders_and_rejects_unusable_inputs(
    paths: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--out", str(paths.out), "--hil", str(paths.hil), "--bacnet", str(paths.bacnet)]
    argv += ["--mqtt", str(paths.mqtt), "--hardware-map", str(paths.hardware_map)]
    assert gen.main(argv) == 0
    assert "hil.mqtt.release.mtls" in capsys.readouterr().out
    (paths.mqtt / "apps" / "mqtt_tls" / "sample.yaml").unlink()
    assert gen.main(argv) == 2
    err = capsys.readouterr().err
    assert "missing:" in err and "apps/mqtt_tls/sample.yaml" in err
    spaced = tmp_path / "a b"
    assert gen.main([*argv[:1], str(spaced), *argv[2:]]) == 2
    assert "without whitespace" in capsys.readouterr().err


def _zephyr_base() -> Path | None:
    for candidate in (os.environ.get("ZEPHYR_BASE"), HIL_ROOT.parents[1] / "zephyr"):
        if (
            candidate
            and (Path(candidate) / "scripts" / "schemas" / "twister" / "testsuite-schema.yaml").is_file()
        ):
            return Path(candidate)
    return None


def test_twister_loads_the_rendered_scenarios(paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Twister's schema and scenario loader accept the files and see the values gen.py wrote."""
    zephyr = _zephyr_base()
    if zephyr is None:
        pytest.skip("no Zephyr tree: set ZEPHYR_BASE (or use a checkout inside its west workspace)")
    monkeypatch.setenv("ZEPHYR_BASE", str(zephyr))
    monkeypatch.syspath_prepend(str(zephyr / "scripts" / "pylib" / "twister"))  # restored afterwards
    try:
        scl = importlib.import_module("scl")
        parser_class = importlib.import_module("twisterlib.config_parser").TwisterConfigParser
    except ImportError as e:
        pytest.skip(f"Twister's loader needs {e.name} (pip install -r $ZEPHYR_BASE/scripts/requirements.txt)")
    schema = scl.yaml_load(str(zephyr / "scripts" / "schemas" / "twister" / "testsuite-schema.yaml"))
    gen.render(paths, it2=True)
    for app in ("bacnet", "mqtt"):
        parser = parser_class(str(paths.alt_root(app) / "sample.yaml"), schema)
        parser.load()
        assert list(parser.scenarios) == IT1[app] + IT2[app]
        for name, entry in rendered(paths, app).items():
            scenario = parser.get_scenario(name)
            assert scenario["harness"] == "pytest" and scenario["timeout"] == 3600
            assert scenario["tags"] == {"hil"} and scenario["platform_allow"] == {"nucleo_f767zi"}
            assert scenario["extra_args"] == entry["extra_args"]
            assert scenario["harness_config"] == entry["harness_config"]
            assert scenario["extra_dtc_overlay_files"] == [] and scenario["conf_files"] == []

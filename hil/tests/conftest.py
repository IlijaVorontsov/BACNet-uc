"""Fixtures, options and marks of the HIL/SIL suite (see hil/README.md and HIL.md 8.2).

Selecting a bench:
    pytest                      unit tests; bench from $HIL_BENCH if set
    pytest --sil                SIL tier: hil/host/bench-sil.yml (bacserv stand-in DUT)
    pytest --sil --sil-dut mqtt=<build>/zephyr/zephyr.exe
                                SIL against a native_sim DUT on TAP zeth in netns lan-a
    pytest --hil --bench B.yml  the real rig described by B.yml
    Twister (D31)               --twister-config/--twister-harness: implies --hil, the bench
                                comes from the reserved DUT's hil_bench:<path> fixture and
                                the session start requests Twister's ``dut`` (the only flash)

Tests that need a bench skip with the reason when there is none; ``hil_only``, ``timing``,
``slow``, ``destructive``, ``mstp``, ``instrumented`` and ``variant`` skip per the rules in
:func:`pytest_runtest_setup`. Everything that touches network namespaces needs root.

Rig gate (HIL.md 9): tests marked ``rig(gate=True)`` run first; with ``--hil`` a gate
failure skips every later test as a *rig fault*, reported separately. With ``--hil`` (and in
Twister mode) a run in which no test executed fails.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pytest

from hilrig import bacnet as bacnet_mod
from hilrig import flash as flash_mod
from hilrig import netns as nsmod
from hilrig.bench import Bench, BenchError, client_id_from_uid, native_uid
from hilrig.capture import Capture, CaptureError
from hilrig.console import Console, SerialConsole, TwisterConsole
from hilrig.dutctl import DutControl, DutLink, SessionStart
from hilrig.markexpr import MarkExprError, compile_expr
from hilrig.mqtt import Device, MqttClient
from hilrig.netns import Topology
from hilrig.pki import CertPair, Pki
from hilrig.release import DutImage
from hilrig.rigconfig import RigConfig, golden, validate
from hilrig.services import BrokerTls, Chrony, Dnsmasq, Mosquitto, RigServices, TlsFront, TlsServer
from hilrig.sildut import SIL_DEVICE_ID, SIL_MAC, SilDut
from hilrig.smp import HARNESS_ENV, Smp, harness_problem
from hilrig.stim import Stim, StimNoDevice
from rig_skips import needs_root, needs_tools

if TYPE_CHECKING:
    from hilrig.la import LogicAnalyzer

HIL_ROOT = Path(__file__).resolve().parents[1]
SIL_BENCH = HIL_ROOT / "host" / "bench-sil.yml"
DEFAULT_BPF = (
    "udp port 47808 or tcp port 8883 or udp port 67 or udp port 68 or udp port 53 "
    "or udp port 123 or udp port 1337 or arp or icmp"
)
APPS = ("bacnet", "mqtt")
SCHEMAS_ENV = "HIL_BACNET_SCHEMAS"

BENCH = pytest.StashKey["Bench | None"]()
ARTIFACTS = pytest.StashKey[Path]()
MODE = pytest.StashKey["Literal['hil', 'sil'] | None"]()
TWISTER = pytest.StashKey[bool]()
IMAGE = pytest.StashKey["DutImage | None"]()
SIL_EXES = pytest.StashKey["dict[str, Path]"]()
RIG_FAULT = pytest.StashKey["str | None"]()
SESSION = pytest.StashKey["SessionStart"]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("hil", "HIL rig")
    group.addoption(
        "--bench", default=os.environ.get("HIL_BENCH"), help="bench.yml of the rig (default: $HIL_BENCH)"
    )
    group.addoption("--hil", action="store_true", help="run against the real rig given by --bench")
    group.addoption(
        "--sil",
        action="store_true",
        help=f"run the SIL tier ({SIL_BENCH.relative_to(HIL_ROOT)} unless --bench)",
    )
    group.addoption(
        "--artifacts",
        default=os.environ.get("HIL_ARTIFACTS"),
        help="artifacts directory (default: $HIL_ARTIFACTS, else /tmp/hil-artifacts/<UTC time>)",
    )
    group.addoption("--enable-slow", action="store_true", help="run tests marked slow")
    group.addoption("--enable-destructive", action="store_true", help="run tests marked destructive")
    group.addoption(
        "--cycles",
        type=int,
        default=int(os.environ.get("HIL_CYCLES", "10")),
        help="power-cycle and reset loop count (default 10 locally, 20 nightly, 50 weekly)",
    )
    group.addoption(
        "--hil-select", default=None, metavar="EXPR", help="marker expression that narrows the scenario's -m"
    )
    group.addoption(
        "--dut-build", default=None, help="local runs: build dir of the image under test (flashed)"
    )
    group.addoption("--no-flash", action="store_true", help="local runs: do not flash --dut-build")
    group.addoption(
        "--dut-app", choices=APPS, default=None, help="the app on the DUT when no build dir is given"
    )
    group.addoption(
        "--sil-dut",
        action="append",
        default=[],
        metavar="APP=ZEPHYR_EXE",
        help="SIL: a native_sim TAP build to run as the DUT (bacnet=... and/or mqtt=...; $HIL_SIL_DUT)",
    )
    group.addoption("--release-build", action="append", default=[], help="SEC-01: a release build dir")
    group.addoption("--plain-build", action="append", default=[], help="SEC-01: a plain app build dir")
    group.addoption(
        "--mqtt-checkout",
        default=os.environ.get("HIL_MQTT_CHECKOUT"),
        help="S-03: the MQTT branch checkout inside a west workspace ($HIL_MQTT_CHECKOUT)",
    )


def twister_mode(config: pytest.Config) -> bool:
    """D31: Twister 4.4.2 passes --twister-config; --twister-harness is treated the same."""
    return bool(config.getoption("twister_config", None) or config.getoption("twister_harness", False))


def _twister_device(config: pytest.Config) -> Any:
    harness_config = getattr(config, "twister_harness_config", None)
    return harness_config.devices[0] if harness_config and harness_config.devices else None


def _twister_bench(config: pytest.Config) -> str | None:
    """The bench path of the reserved DUT's ``hil_bench:<path>`` fixture (map.yml, D31).

    Under ``--collect-only`` the harness plugin builds no device configuration, so a listing
    runs without a bench instead of failing.
    """
    device = _twister_device(config)
    for fixture in (device.fixtures or []) if device else []:
        if fixture.startswith("hil_bench:"):
            return str(fixture.split(":", 1)[1])
    if device is None and config.getoption("collectonly"):
        return None
    raise pytest.UsageError("Twister mode: the reserved DUT has no hil_bench:<path> fixture (map.yml)")


def _sil_exes(config: pytest.Config) -> dict[str, Path]:
    specs = list(config.getoption("--sil-dut")) or [
        s for s in os.environ.get("HIL_SIL_DUT", "").split(";") if s
    ]
    exes: dict[str, Path] = {}
    for spec in specs:
        app, sep, path = spec.partition("=")
        if not sep or app not in APPS:
            raise pytest.UsageError(f"--sil-dut {spec!r}: expected bacnet=<zephyr.exe> or mqtt=<zephyr.exe>")
        if not Path(path).is_file():
            raise pytest.UsageError(f"--sil-dut {spec!r}: {path} is not a file")
        exes[app] = Path(path).resolve()
    return exes


def _sil_dut_bench(bench: Bench, exes: dict[str, Path]) -> Bench:
    """The SIL bench for native_sim DUTs: TAP topology, fixed MAC and hwinfo id (hilrig.sildut)."""
    uid = native_uid(SIL_DEVICE_ID)
    client_id = None
    if "mqtt" in exes:
        image = _image_of_exe(exes["mqtt"])
        client_id = image.client_id if image else None
    dut = dataclasses.replace(
        bench.dut,
        board="native_sim/native/64",
        mac=SIL_MAC,
        uid=uid.hex(),
        mqtt_client_id=client_id or client_id_from_uid(uid),
    )
    return dataclasses.replace(bench, dut=dut)


def _image_of_exe(exe: Path) -> DutImage | None:
    build = exe.parent.parent  # <build>/zephyr/zephyr.exe
    try:
        return DutImage.from_build(build)
    except (FileNotFoundError, ValueError):
        return None


@pytest.hookimpl(trylast=True)  # after the Twister harness plugin has built its config (D31)
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "instrumented: needs an instrumented image (-S hil), tier instrumented"
    )
    config.addinivalue_line("markers", "rig(gate=False): rig self-test; gate=True: a failure is a rig fault")
    config.addinivalue_line("markers", "variant: runs on a release image plus one documented symbol (D24)")
    config.addinivalue_line("markers", "sil: SIL tier (native_sim or stand-ins; no timing asserts)")
    config.addinivalue_line(
        "markers", "cycles(base_s, per_cycle_s): timeout = base_s + per_cycle_s x --cycles"
    )
    twister = twister_mode(config)
    hil, sil = config.getoption("--hil") or twister, config.getoption("--sil")
    if hil and sil:
        raise pytest.UsageError("--hil and --sil are exclusive")
    exes = _sil_exes(config)
    path = _twister_bench(config) if twister else config.getoption("--bench") or (SIL_BENCH if sil else None)
    bench = None
    if path:
        try:
            bench = Bench.load(path)
        except BenchError as e:
            raise pytest.UsageError(str(e)) from None
        wanted = "hil" if hil else "sil" if sil else bench.mode
        if bench.mode != wanted:
            raise pytest.UsageError(f"{path} is a {bench.mode} bench, but --{wanted} was given")
    if exes and (bench is None or bench.mode != "sil"):
        raise pytest.UsageError("--sil-dut needs --sil")
    if exes and bench is not None:
        bench = _sil_dut_bench(bench, exes)
    config.stash[BENCH] = bench
    config.stash[MODE] = bench.mode if bench else None
    config.stash[TWISTER] = twister
    config.stash[SIL_EXES] = exes
    config.stash[IMAGE] = _session_image(config, bench)
    config.stash[RIG_FAULT] = None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifacts = Path(config.getoption("--artifacts") or Path("/tmp/hil-artifacts") / stamp)
    config.stash[ARTIFACTS] = artifacts
    if expr := config.getoption("--hil-select"):
        try:
            compile_expr(expr)
        except MarkExprError as e:
            raise pytest.UsageError(str(e)) from None


def _session_image(config: pytest.Config, bench: Bench | None) -> DutImage | None:
    """The image under test on hardware: Twister's build, --dut-build, or --dut-app."""
    if bench is None or bench.mode == "sil":
        return DutImage("standin") if bench is not None and bench.dut.board == "bacserv" else None
    device = _twister_device(config) if config.stash[TWISTER] else None
    build = Path(device.app_build_dir) if device is not None else None
    build = build or (Path(config.getoption("--dut-build")) if config.getoption("--dut-build") else None)
    if build is not None:
        try:
            return DutImage.from_build(build)
        except (FileNotFoundError, ValueError) as e:
            raise pytest.UsageError(f"--dut-build: {e}") from None
    app = config.getoption("--dut-app")
    return DutImage(app) if app else None


def pytest_report_header(config: pytest.Config) -> str:
    bench, image = config.stash[BENCH], config.stash[IMAGE]
    where = f"{bench.name} ({bench.mode}, {bench.dut.board})" if bench else "none (unit tests only)"
    what = f"; image {image.kind}{' instrumented' if image.instrumented else ''}" if image else ""
    sil = config.stash[SIL_EXES]
    duts = f"; SIL DUTs {', '.join(f'{a}={p}' for a, p in sil.items())}" if sil else ""
    return f"hil: bench {where}{what}{duts}; artifacts {config.stash[ARTIFACTS]}"


# ---- collection -----------------------------------------------------------------------------
def _gate(item: pytest.Item) -> bool:
    mark = item.get_closest_marker("rig")
    return bool(mark and mark.kwargs.get("gate"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    sil_dir = HIL_ROOT / "tests" / "sil"
    for item in items:
        if sil_dir in Path(item.path).parents:
            item.add_marker(pytest.mark.sil)  # S-01, S-02 (tests/sil): the sil tier
        if mark := item.get_closest_marker("cycles"):
            base, per_cycle = mark.args
            item.add_marker(pytest.mark.timeout(base + per_cycle * config.getoption("--cycles")))
    if expr := config.getoption("--hil-select"):
        predicate = compile_expr(expr)
        keep = [i for i in items if predicate(frozenset(m.name for m in i.iter_markers()))]
        dropped = [i for i in items if i not in keep]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = keep
    items.sort(key=lambda i: 0 if _gate(i) else 1)  # stable: gates first, in file order


def pytest_runtest_setup(item: pytest.Item) -> None:
    config = item.config
    bench, mode, image = config.stash[BENCH], config.stash[MODE], config.stash[IMAGE]
    fault = config.stash[RIG_FAULT]
    if fault and mode == "hil":
        pytest.skip(f"rig fault: {fault}; the DUT is not tested on a failed rig")
    if item.get_closest_marker("hil_only") and mode != "hil":
        pytest.skip("hil_only: needs the real rig (pytest --hil --bench bench.yml)")
    if item.get_closest_marker("timing") and mode != "hil":
        pytest.skip("timing: asserted on hardware only")
    if item.get_closest_marker("slow") and not config.getoption("--enable-slow"):
        pytest.skip("slow: needs --enable-slow")
    if item.get_closest_marker("destructive") and not config.getoption("--enable-destructive"):
        pytest.skip("destructive: needs --enable-destructive (use the spare DUT)")
    if item.get_closest_marker("mstp") and (bench is None or bench.mstp is None):
        pytest.skip("mstp: the bench has no MS/TP bus (mstp section in bench.yml)")
    if item.get_closest_marker("instrumented") and mode == "hil" and not (image and image.instrumented):
        pytest.skip("instrumented: the image under test is not an instrumented build (-S hil; --dut-build)")
    if item.get_closest_marker("variant") and not (image and image.variant):
        pytest.skip("variant: needs a variant image (D24, e.g. APP_MQTT_PUBLISH_INTERVAL_SEC=120)")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    """ERR -19 (ENODEV) from the stimulus: the hardware is absent, so the test skips."""
    try:
        return (yield)
    except StimNoDevice as e:
        pytest.skip(f"stimulus hardware absent: {e}")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report: pytest.TestReport = yield
    config = item.config
    if report.failed and _gate(item) and config.stash[MODE] == "hil" and not config.stash[RIG_FAULT]:
        config.stash[RIG_FAULT] = f"gate {item.nodeid} failed ({report.when})"
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if BENCH not in config.stash:
        return
    reports = config.pluginmanager.get_plugin("terminalreporter")
    stats = reports.stats if reports is not None else {}
    executed = sum(len(stats.get(key, [])) for key in ("passed", "failed", "xfailed", "xpassed"))
    fault = config.stash[RIG_FAULT]
    artifacts = config.stash[ARTIFACTS]
    if fault:
        (artifacts / "rig").mkdir(parents=True, exist_ok=True)
        (artifacts / "rig" / "rig-fault.json").write_text(json.dumps({"rig_fault": fault}, indent=2) + "\n")
    hil = config.stash[MODE] == "hil" or config.stash[TWISTER]
    # D31: an all-skipped HIL run is not green, and neither is one whose -m/-k/--hil-select
    # deselected everything (pytest would exit 5, which Twister records as a skip)
    if hil and executed == 0 and not config.getoption("collectonly"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    fault = config.stash.get(RIG_FAULT, None)
    if fault:
        terminalreporter.section("rig fault")
        terminalreporter.write_line(f"RIG FAULT (not a DUT failure): {fault}")


def requires_root() -> None:
    """Skip the calling fixture's tests unless running as root."""
    if needs_root.args[0]:
        pytest.skip(needs_root.kwargs["reason"])


def requires_tools(*tools: str) -> None:
    """Skip the calling fixture's tests unless every program in ``tools`` is on PATH."""
    mark = needs_tools(*tools)
    if mark.args[0]:
        pytest.skip(mark.kwargs["reason"])


# ---- session ------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def artifacts(pytestconfig: pytest.Config) -> Path:
    """Directory for this run's pcaps, logs and certificates (uploaded by CI)."""
    path = pytestconfig.stash[ARTIFACTS]
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def selected_bench(pytestconfig: pytest.Config) -> Bench | None:
    """The bench of this run, or None; for fixtures that fall back to a fake without one."""
    return pytestconfig.stash[BENCH]


@pytest.fixture(scope="session")
def bench(pytestconfig: pytest.Config) -> Bench:
    """The bench under test; skips the test when none was selected."""
    selected = pytestconfig.stash[BENCH]
    if selected is None:
        pytest.skip("no bench: run with --sil, or --hil --bench bench.yml (or set HIL_BENCH)")
    return selected


@pytest.fixture(scope="session")
def cycles(pytestconfig: pytest.Config) -> int:
    """--cycles: loop count of the power-cycle and reset tests."""
    count: int = pytestconfig.getoption("--cycles")
    return count


@pytest.fixture(scope="session")
def netns(bench: Bench) -> Iterator[Topology]:
    """The rig topology for the bench, brought up for the session (and down, if we did)."""
    requires_root()
    topology = Topology(
        dut_iface=bench.net.dut_iface,
        sil=bench.dut.board == "native_sim/native/64",
        standin=bench.dut.board == "bacserv",
    )
    with topology.session():
        yield topology


@pytest.fixture(scope="session")
def pki(artifacts: Path) -> Pki:
    """Test CA, DUT client certificate and this run's server/client certificates."""
    requires_tools("openssl")
    return Pki.generate(artifacts / "pki")


@pytest.fixture(scope="session")
def services(netns: Topology, bench: Bench, pki: Pki, artifacts: Path) -> Iterator[RigServices]:
    """dnsmasq (DUT reservation), mosquitto (test PKI, key log if possible) and chrony in svc."""
    requires_tools("dnsmasq", "mosquitto")
    svc, rundir = netns.ns("svc"), artifacts / "services"
    good = pki.servers["good"]
    rig = RigServices(
        Dnsmasq(svc, rundir, dut_mac=bench.dut.mac, dut_ip=bench.dut.ip, any_mac=netns.sil),
        Mosquitto(svc, rundir, BrokerTls(pki.ca, good.cert, good.key, crl=pki.crl)),
        Chrony(svc, rundir) if Chrony.available() else None,
    )
    with rig:
        yield rig


@pytest.fixture(scope="session")
def stim(bench: Bench, artifacts: Path) -> Iterator[Stim]:
    """The stimulus board (protocol v0, port opened exclusively), safe before and after."""
    if bench.stim is None:
        pytest.skip("the bench has no stimulus board (stim section in bench.yml)")
    with Stim(bench.stim.serial, baud=bench.stim.baud, log=artifacts / "stim.log") as board:
        missing = sorted(set(bench.stim.chans) - set(board.info().chans))
        if missing:
            pytest.fail(f"stimulus firmware lacks channels listed in bench.yml: {', '.join(missing)}")
        board.safe()
        yield board
        board.safe()


@pytest.fixture(autouse=True)
def _stim_safe(request: pytest.FixtureRequest) -> Iterator[None]:
    """Put the stimulus into the safe state before and after every test, if there is one."""
    bench = request.config.stash[BENCH]
    if bench is None or bench.stim is None:
        yield
        return
    board: Stim = request.getfixturevalue("stim")
    board.safe()
    yield
    board.safe()


@pytest.fixture(scope="session")
def la(bench: Bench) -> LogicAnalyzer:
    """The bench's logic analyzer (hilrig.la); tests call ``la.acquire(seconds, workdir)``."""
    if bench.la is None:
        pytest.skip("the bench has no logic analyzer (la section in bench.yml)")
    from hilrig.la import open_analyzer  # numpy only where an analyzer is used

    return open_analyzer(bench.la.driver, bench.la.channels)


@pytest.fixture(scope="session")
def bacnet(netns: Topology) -> bacnet_mod.Bacnet:
    """bacnet-stack client tools in netns svc (``.foreign()`` for subnet B clients)."""
    tools = bacnet_mod.tools_dir()
    if not (tools / "bacwi").exists():
        pytest.skip(f"bacnet-stack tools not found in {tools} (set {bacnet_mod.TOOLS_ENV})")
    return bacnet_mod.Bacnet(netns.ns("svc"), "svc0", tools=tools)


# ---- the DUT image, its console and its management plane ------------------------------------
class SilDuts:
    """The native_sim DUTs of a SIL run; one runs at a time at 192.0.2.10 (TAP zeth)."""

    def __init__(self, exes: dict[str, Path], netns: Topology, artifacts: Path) -> None:
        self.exes, self.netns, self.artifacts = exes, netns, artifacts
        self.duts: dict[str, SilDut] = {}
        self.active: str | None = None
        self.ready: dict[str, int] = {}  # app -> SilDut.generation last seen answering

    def use(self, app: str) -> SilDut:
        """Make ``app``'s DUT the running one (stopping the other) and return it."""
        if self.active == app and self.duts[app].running():
            return self.duts[app]
        for other in self.duts.values():
            other.stop()
        if app not in self.duts:
            self.duts[app] = SilDut(
                self.exes[app], self.artifacts / "sil-dut" / app, netns=self.netns.ns("lan-a")
            )
        self.duts[app].start()
        self.active = app
        return self.duts[app]

    def ensure_any(self) -> SilDut:
        """Keep the running DUT, or start the first one (a capture on zeth needs carrier)."""
        return self.use(self.active if self.active else next(iter(self.exes)))

    def stop(self) -> None:
        for dut in self.duts.values():
            dut.stop()
            dut.console.shutdown()


@pytest.fixture(scope="session")
def sil_duts(
    pytestconfig: pytest.Config, netns: Topology, services: RigServices, artifacts: Path
) -> Iterator[SilDuts]:
    """The --sil-dut executables, started on demand inside lan-a (services first: DHCP)."""
    exes = pytestconfig.stash[SIL_EXES]
    if not exes:
        pytest.skip("SIL without a native_sim DUT: pass --sil-dut bacnet=<zephyr.exe> or mqtt=<zephyr.exe>")
    duts = SilDuts(exes, netns, artifacts)
    yield duts
    duts.stop()


def sil_image(pytestconfig: pytest.Config, app: Literal["bacnet", "mqtt"]) -> DutImage:
    """The DutImage of a --sil-dut executable (from its build directory when present)."""
    exe = pytestconfig.stash[SIL_EXES][app]
    return _image_of_exe(exe) or DutImage(app)


@pytest.fixture
def app_image(request: pytest.FixtureRequest, pytestconfig: pytest.Config) -> DutImage:
    """The image of the app a test is parametrised with (indirect: ``bacnet`` or ``mqtt``).

    On hardware the test skips unless that app is on the DUT; in SIL the matching
    native_sim DUT is started (and the bacserv stand-in is refused: it is not the product).
    """
    app: Literal["bacnet", "mqtt"] | None = getattr(request, "param", None)
    if app is None or app not in APPS:
        raise pytest.UsageError(f"{request.node.nodeid}: parametrise app_image with one of {APPS}")
    bench = pytestconfig.stash[BENCH]
    if bench is None:
        pytest.skip("no bench: run with --sil --sil-dut ..., or --hil --bench bench.yml")
    if bench.mode == "sil":
        if app not in pytestconfig.stash[SIL_EXES]:
            pytest.skip(
                f"SIL: this test needs a native_sim {app} DUT (--sil-dut {app}=<build>/zephyr/zephyr.exe)"
            )
        duts: SilDuts = request.getfixturevalue("sil_duts")
        dut, built = duts.use(app), sil_image(pytestconfig, app)
        if app == "bacnet" and duts.ready.get(app) != dut.generation:
            _sil_bacnet_ready(request, bench, built)
            duts.ready[app] = dut.generation
        return built
    image = pytestconfig.stash[IMAGE]
    if image is None:
        pytest.skip("the image under test is unknown: give --dut-build DIR (or --dut-app)")
    if image.app != app:
        pytest.skip(f"this {app} test does not apply: the DUT runs the {image.app} image")
    return image


def _sil_bacnet_ready(request: pytest.FixtureRequest, bench: Bench, image: DutImage) -> None:
    """After a SIL BACnet boot: wait for its I-Am and the known state, as the session start
    does on hardware (7.3). The --flash file keeps the pushed documents across restarts."""
    services: RigServices = request.getfixturevalue("services")
    link = DutLink(bench, image, dnsmasq=services.dnsmasq, bacnet=request.getfixturevalue("bacnet"))
    if not link.answers():
        link.wait_app(link.mark(), 60)
    request.getfixturevalue("rig_config")


@pytest.fixture(scope="session")
def dut_console(request: pytest.FixtureRequest, bench: Bench, artifacts: Path) -> Iterator[Console | None]:
    """The DUT's console for the session (hardware): Twister's ``dut`` or pyserial (8.2).

    Recorded always; input only on instrumented images (the release tier records only).
    """
    config = request.config
    image = config.stash[IMAGE]
    writable = bool(image and image.instrumented)
    if bench.mode != "hil":
        yield None
        return
    if config.stash[TWISTER]:
        console: Console = TwisterConsole(
            request.getfixturevalue("dut"), artifacts / "console.log", writable=writable
        )
    elif bench.dut.serial:
        console = SerialConsole(bench.dut.serial, artifacts / "console.log", writable=writable)
        console.open(retry_s=5)
    else:
        yield None
        return
    yield console
    console.shutdown()


@pytest.fixture
def console(request: pytest.FixtureRequest, app_image: DutImage, bench: Bench) -> Console:
    """The console of the DUT under test (hardware session console, or the SIL process's)."""
    if bench.mode == "sil":
        dut: SilDut = request.getfixturevalue("sil_duts").use(app_image.app)
        return dut.console
    session_console: Console | None = request.getfixturevalue("dut_console")
    if session_console is None:
        pytest.skip("the bench has no DUT console (dut.serial in bench.yml)")
    return session_console


@pytest.fixture(scope="session")
def smp(request: pytest.FixtureRequest, bench: Bench, netns: Topology) -> Iterator[Smp]:
    """SMP to the BACnet DUT over UDP 1337 from netns svc (the harness client, 3.2)."""
    config = request.config
    if problem := harness_problem():
        pytest.skip(problem)
    if bench.mode == "sil":
        if "bacnet" not in config.stash[SIL_EXES]:
            pytest.skip("SIL: SMP needs a native_sim BACnet DUT (--sil-dut bacnet=...)")
        request.getfixturevalue("sil_duts").use("bacnet")
    elif (image := config.stash[IMAGE]) is None or image.app != "bacnet":
        pytest.skip("SMP needs the BACnet image on the DUT (the MQTT image has no SMP)")
    with Smp(bench.dut.ip, netns=netns.ns("svc")) as client:
        client.wait_up(60)
        yield client


def branch_schemas() -> Path | None:
    """The BACnet branch's schemas/ (``$HIL_BACNET_SCHEMAS``, else next to the harness)."""
    if path := os.environ.get(SCHEMAS_ENV):
        return Path(path)
    harness = os.environ.get(HARNESS_ENV)
    if harness:
        root = Path(harness)
        root = root.parent if root.name == "src" else root
        if (root.parent / "schemas").is_dir():
            return root.parent / "schemas"
    return None


@pytest.fixture(scope="session")
def rig_config(smp: Smp, bench: Bench, artifacts: Path) -> RigConfig:
    """Known state (D25): golden device/io/apps.json pushed, verified, reloaded, rebooted if needed."""
    schemas = branch_schemas()
    docs = golden(bench, schemas / "examples" if schemas else None)
    notes = validate(docs, schemas) if schemas else [f"no schemas: set {SCHEMAS_ENV} or {HARNESS_ENV}"]
    config = RigConfig(smp, docs)
    report = config.apply()
    rig = artifacts / "rig"
    rig.mkdir(parents=True, exist_ok=True)
    record = {"docs": docs, "notes": notes, "report": dataclasses.asdict(report)}
    (rig / "rig_config.json").write_text(json.dumps(record, indent=2, default=str) + "\n")
    return config


# ---- session start (HIL.md 7.3, D31) --------------------------------------------------------
def run_guard(bench: Bench) -> flash_mod.GuardResult:
    """R-07 before a flash: refuse (pytest.fail, a rig fault) on any mismatch."""
    if not bench.dut.probe:
        pytest.fail("rig fault: bench.yml has no dut.probe, so the flash guard cannot run")
    try:
        identity, _ = flash_mod.read_identity(bench.dut.probe)
    except flash_mod.FlashError as e:
        pytest.fail(f"rig fault: flash guard: {e}")
    result = flash_mod.check_identity(identity, bench)
    if not result.ok:
        pytest.fail(f"rig fault: flash guard refused the board: {'; '.join(result.errors)}")
    return result


def _session_link(
    request: pytest.FixtureRequest, bench: Bench, image: DutImage
) -> tuple[DutLink, MqttClient | None]:
    """A DutLink for the session start (its own MQTT observer: the ``mqtt`` fixture is per test)."""
    services: RigServices = request.getfixturevalue("services")
    observer = None
    device = None
    if image.app == "mqtt" and bench.dut.mqtt_client_id:
        netns: Topology = request.getfixturevalue("netns")
        observer = MqttClient(
            "127.0.0.1", services.broker.observer_port, netns=netns.ns("svc"), client_id="hil-start"
        )
        device = Device(observer.connect(), bench.dut.mqtt_client_id).observe()
    bacnet = request.getfixturevalue("bacnet") if image.app == "bacnet" else None
    return DutLink(bench, image, dnsmasq=services.dnsmasq, bacnet=bacnet, mqtt=device), observer


def _flash_in_capture(
    request: pytest.FixtureRequest, bench: Bench, image: DutImage | None, start: SessionStart
) -> None:
    """Flash (Twister's ``dut`` or west flash) with a capture running, then wait for the network."""
    config = request.config
    netns: Topology = request.getfixturevalue("netns")
    services: RigServices = request.getfixturevalue("services")
    artifacts = config.stash[ARTIFACTS]
    link, observer = _session_link(request, bench, image) if image and image.app in APPS else (None, None)
    cap = Capture(
        netns.capture_iface,
        netns.ns("lan-a"),
        DEFAULT_BPF,
        artifacts / "pcap" / "session-start.pcapng",
        sentinel_netns=netns.ns("svc"),
        keylog=services.broker.keylog,
    )
    try:
        cap.start()
        start.capture = cap.path
    except CaptureError as e:  # no carrier before the flash: SYS-01 then has no wire view
        start.notes.append(f"no capture of the flash: {e}")
    mark = link.mark() if link else None
    if not config.stash[TWISTER]:  # locally the console is open: the boot lines come after this
        console: Console | None = request.getfixturevalue("dut_console")
        start.console_mark = console.mark() if console else 0
    try:
        began = time.monotonic()
        if config.stash[TWISTER]:
            request.getfixturevalue("dut")  # Twister flashes and connects the serial port here
            start.flash = flash_mod.FlashResult(
                0, time.monotonic() - began, time.time(), "Twister dut fixture"
            )
        else:
            assert bench.dut.probe is not None
            start.flash = flash_mod.flash(Path(config.getoption("--dut-build")), bench.dut.probe)
        start.flash_end = start.flash.ended
        if start.flash.returncode != 0:
            pytest.fail(
                f"rig fault: west flash failed ({start.flash.returncode}): {start.flash.output[-500:]}"
            )
        if link is not None and mark is not None:
            start.online_s = link.wait_app(mark, start.flash.seconds + 60) - start.flash.seconds
    finally:
        cap.stop()
        if observer is not None:
            observer.close()


@pytest.fixture(scope="session", autouse=True)
def hil_session(request: pytest.FixtureRequest) -> Iterator[SessionStart | None]:
    """Session start with --hil / Twister: stimulus check, R-07 guard, flash, network, rig_config.

    The sequence of HIL.md 7.3. In Twister mode the flash is Twister's ``dut`` fixture (its
    only flash, D31); locally it is ``west flash`` of --dut-build. The flash runs inside a
    capture, so SYS-01 checks the boot of the flashed image. R-02b follows as a gate test.
    """
    config = request.config
    bench = config.stash[BENCH]
    if config.stash[MODE] != "hil" or bench is None:
        yield None
        return
    start, image = SessionStart(), config.stash[IMAGE]
    config.stash[SESSION] = start
    if bench.stim is not None:
        board: Stim = request.getfixturevalue("stim")
        info = board.info()
        profile = info.profile or "P1"
        if info.proto != 0 or profile != bench.dut.profile:
            pytest.fail(
                f"rig fault: stimulus proto={info.proto} profile={profile}, bench {bench.dut.profile}"
            )
        board.safe()
    flashing = config.stash[TWISTER] or (
        config.getoption("--dut-build") and not config.getoption("--no-flash")
    )
    if not config.stash[TWISTER]:
        request.getfixturevalue("dut_console")  # record the boot of the image flashed next
    if flashing:
        if not config.stash[TWISTER] or (bench.dut.probe and flash_mod.find_openocd()):
            start.guard = run_guard(bench)
        _flash_in_capture(request, bench, image, start)
    else:
        start.notes.append("no flash: testing the image already on the DUT")
    if image is not None and image.app == "bacnet":
        if start.online_s is None:
            link, _ = _session_link(request, bench, image)
            start.online_s = link.wait_app(link.mark(), 60)
        request.getfixturevalue("rig_config")
    yield start


# ---- per test -----------------------------------------------------------------------------
@pytest.fixture
def capture(
    request: pytest.FixtureRequest, netns: Topology, bench: Bench, artifacts: Path
) -> Iterator[Capture]:
    """A running capture on the DUT's wire (mark ``capture(bpf)`` narrows the filter).

    The pcapng is kept under ``<artifacts>/pcap``; with the ``services`` fixture active the
    broker's TLS key log is injected into it (and those of ``tls_server``/``tls_front``). A
    malformed frame from the DUT fails the test. A SIL DUT is started first: the capture's
    readiness barrier needs carrier on zeth.
    """
    requires_tools("tshark", "editcap")
    if "app_image" in request.fixturenames:
        request.getfixturevalue("app_image")
    elif netns.sil and request.config.stash[SIL_EXES]:
        request.getfixturevalue("sil_duts").ensure_any()
    marker = request.node.get_closest_marker("capture")
    bpf = marker.args[0] if marker and marker.args else DEFAULT_BPF
    # iface="br-a": tests that take the DUT's link down capture on the bridge, which stays up
    iface = marker.kwargs.get("iface", netns.capture_iface) if marker else netns.capture_iface
    keylog = request.getfixturevalue("services").broker.keylog if "services" in request.fixturenames else None
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)
    cap = Capture(
        iface,
        netns.ns("lan-a"),
        bpf,
        artifacts / "pcap" / f"{name}.pcapng",
        sentinel_netns=netns.ns("svc"),
        keylog=keylog,
    )
    with cap:
        yield cap
    bad = cap.rows(f"_ws.malformed && ip.src == {bench.dut.ip}", "frame.number")
    if bad:
        pytest.fail(f"malformed frames from the DUT in {cap.path}: {[r['frame.number'] for r in bad]}")


@pytest.fixture
def mqtt(services: RigServices, bench: Bench, netns: Topology) -> Iterator[Device]:
    """Observer/commander for the DUT's topics, on the broker's local listener in svc."""
    if not bench.dut.mqtt_client_id:
        pytest.skip("the bench has no dut.mqtt_client_id")
    client = MqttClient(
        "127.0.0.1", services.broker.observer_port, netns=netns.ns("svc"), client_id="hil-observer"
    )
    with client:
        yield Device(client, bench.dut.mqtt_client_id).observe()


@pytest.fixture
def dut_link(
    request: pytest.FixtureRequest, app_image: DutImage, bench: Bench, services: RigServices
) -> DutLink:
    """Readiness checks for the image under test: DHCP lease, I-Am or CONNACK, HIL-READY."""
    console = request.getfixturevalue("console") if bench.mode == "sil" or bench.dut.serial else None
    return DutLink(
        bench,
        app_image,
        dnsmasq=services.dnsmasq,
        bacnet=request.getfixturevalue("bacnet") if app_image.app == "bacnet" else None,
        mqtt=request.getfixturevalue("mqtt") if app_image.app == "mqtt" else None,
        console=console,
    )


def _control(request: pytest.FixtureRequest, link: DutLink, bench: Bench) -> DutControl:
    if bench.mode == "sil":
        assert link.image is not None
        dut: SilDut = request.getfixturevalue("sil_duts").use(link.image.app)
        return DutControl(link, sil=dut)
    return DutControl(link, stim=request.getfixturevalue("stim"), console=link.console)


@pytest.fixture
def dut_reset(request: pytest.FixtureRequest, dut_link: DutLink, bench: Bench) -> DutControl:
    """``reset()`` of HIL.md 7.3 (``stim reset``; in SIL a process restart)."""
    return _control(request, dut_link, bench)


@pytest.fixture
def dut_power(request: pytest.FixtureRequest, dut_link: DutLink, bench: Bench) -> DutControl:
    """``power_cycle()`` of HIL.md 7.3 (E5V through the stimulus; in SIL a process restart)."""
    return _control(request, dut_link, bench)


TlsServerFactory = Callable[..., TlsServer]
TlsFrontFactory = Callable[..., TlsFront]


@pytest.fixture
def tls_server(
    request: pytest.FixtureRequest, services: RigServices, netns: Topology, artifacts: Path
) -> Iterator[TlsServerFactory]:
    """``tls_server(pair, version)``: s_server on 192.0.2.2:8883 with broker.hil.lan there (D33).

    One server at a time; the previous one stops and the name points back at teardown.
    """
    started: list[TlsServer] = []
    capture: Capture | None = (
        request.getfixturevalue("capture") if "capture" in request.fixturenames else None
    )

    def start(pair: CertPair, version: Literal["1.2", "1.3"], ca: Path | None = None) -> TlsServer:
        for old in started:
            old.stop()
        server = TlsServer(
            netns.ns("svc"),
            artifacts / "services",
            cert=pair.cert,
            key=pair.key,
            version=version,
            port=8883,
            bind_ip=nsmod.SVC2_IP,
            ca=ca,
            name=f"s_server-{pair.cert.stem}-tls{version.replace('.', '')}-{len(started)}",
        ).start()
        started.append(server)
        if capture is not None:
            capture.add_keylog(server.keylog)
        services.dnsmasq.point_broker(nsmod.SVC2_IP)
        return server

    try:
        yield start
    finally:  # the servers stop even if the name cannot be pointed back
        try:
            services.dnsmasq.point_broker(nsmod.HOSTS["svc"].ip)
        finally:
            for server in started:
                server.stop()


@pytest.fixture
def tls_front(
    request: pytest.FixtureRequest, services: RigServices, netns: Topology, pki: Pki, artifacts: Path
) -> Iterator[TlsFrontFactory]:
    """``tls_front(version="1.2", client_ca=None)``: the TLS front on 192.0.2.2:8883 (D33).

    It forwards to mosquitto's observer listener, so the ``mqtt`` observer sees the DUT's
    topics; broker.hil.lan points there until teardown.
    """
    started: list[TlsFront] = []
    capture: Capture | None = (
        request.getfixturevalue("capture") if "capture" in request.fixturenames else None
    )

    def start(version: Literal["1.2", "1.3"] = "1.2", client_ca: Path | None = None) -> TlsFront:
        for old in started:
            old.stop()
        good = pki.servers["good"]
        front = TlsFront(
            netns.ns("svc"),
            artifacts / "services",
            cert=good.cert,
            key=good.key,
            version=version,
            upstream=("127.0.0.1", services.broker.observer_port),
            client_ca=client_ca,
            name=f"tls-front-tls{version.replace('.', '')}-{len(started)}",
        ).start()
        started.append(front)
        if capture is not None:
            capture.add_keylog(front.keylog)
        services.dnsmasq.point_broker(nsmod.SVC2_IP)
        return front

    try:
        yield start
    finally:  # the fronts stop even if the name cannot be pointed back
        try:
            services.dnsmasq.point_broker(nsmod.HOSTS["svc"].ip)
        finally:
            for front in started:
                front.stop()


@pytest.fixture
def rig_report(artifacts: Path) -> Callable[[str, dict[str, Any]], Path]:
    """``rig_report(test_id, data)`` writes ``<artifacts>/rig/<test_id>.json``."""

    def write(test_id: str, data: dict[str, Any]) -> Path:
        path = artifacts / "rig" / f"{test_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"t": time.time(), **data}, indent=2, default=str) + "\n")
        return path

    return write


# ---- private topology for module tests ----------------------------------------------------
@pytest.fixture(scope="session")
def unit_net() -> Iterator[Topology]:
    """A private copy of the topology (prefix ``hu-``) for the module tests in tests/unit.

    It needs no bench and never touches the rig topology of a SIL/HIL session.
    """
    requires_root()
    requires_tools("ip")
    topology = Topology(prefix="hu-")
    topology.down()  # leftovers of an aborted run
    with topology.session():
        yield topology

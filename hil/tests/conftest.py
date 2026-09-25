"""Fixtures, options and marks of the HIL/SIL suite (see hil/README.md).

Selecting a bench:
    pytest                      unit tests; bench from $HIL_BENCH if set
    pytest --sil                SIL tier: hil/host/bench-sil.yml (bacserv stand-in DUT)
    pytest --hil --bench B.yml  the real rig described by B.yml

Tests that need a bench (``bench`` fixture and everything built on it) skip with the reason
when there is none; ``hil_only``, ``timing``, ``slow``, ``destructive`` and ``mstp`` skip
per the rules in :func:`pytest_runtest_setup`. Everything that touches network namespaces
needs root and skips without it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hilrig import bacnet as bacnet_mod
from hilrig.bench import Bench, BenchError
from hilrig.capture import Capture
from hilrig.mqtt import Device, MqttClient
from hilrig.netns import Topology
from hilrig.pki import Pki
from hilrig.services import BrokerTls, Chrony, Dnsmasq, Mosquitto, RigServices
from hilrig.stim import Stim
from rig_skips import needs_root, needs_tools

if TYPE_CHECKING:
    from hilrig.la import LogicAnalyzer

HIL_ROOT = Path(__file__).resolve().parents[1]
SIL_BENCH = HIL_ROOT / "host" / "bench-sil.yml"
DEFAULT_BPF = (
    "udp port 47808 or tcp port 8883 or udp port 67 or udp port 68 or udp port 53 "
    "or udp port 123 or arp or icmp"
)

BENCH = pytest.StashKey["Bench | None"]()
ARTIFACTS = pytest.StashKey[Path]()


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


def pytest_configure(config: pytest.Config) -> None:
    hil, sil = config.getoption("--hil"), config.getoption("--sil")
    if hil and sil:
        raise pytest.UsageError("--hil and --sil are exclusive")
    path = config.getoption("--bench") or (SIL_BENCH if sil else None)
    bench = None
    if path:
        try:
            bench = Bench.load(path)
        except BenchError as e:
            raise pytest.UsageError(str(e)) from None
        wanted = "hil" if hil else "sil" if sil else bench.mode
        if bench.mode != wanted:
            raise pytest.UsageError(f"{path} is a {bench.mode} bench, but --{wanted} was given")
    config.stash[BENCH] = bench
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifacts = Path(config.getoption("--artifacts") or Path("/tmp/hil-artifacts") / stamp)
    config.stash[ARTIFACTS] = artifacts


def pytest_report_header(config: pytest.Config) -> str:
    bench = config.stash[BENCH]
    where = f"{bench.name} ({bench.mode}, {bench.dut.board})" if bench else "none (unit tests only)"
    return f"hil: bench {where}; artifacts {config.stash[ARTIFACTS]}"


def pytest_runtest_setup(item: pytest.Item) -> None:
    bench = item.config.stash[BENCH]
    mode = bench.mode if bench else None
    if item.get_closest_marker("hil_only") and mode != "hil":
        pytest.skip("hil_only: needs the real rig (pytest --hil --bench bench.yml)")
    if item.get_closest_marker("timing") and mode != "hil":
        pytest.skip("timing: asserted on hardware only")
    if item.get_closest_marker("slow") and not item.config.getoption("--enable-slow"):
        pytest.skip("slow: needs --enable-slow")
    if item.get_closest_marker("destructive") and not item.config.getoption("--enable-destructive"):
        pytest.skip("destructive: needs --enable-destructive (use the spare DUT)")
    if item.get_closest_marker("mstp") and (bench is None or bench.mstp is None):
        pytest.skip("mstp: the bench has no MS/TP bus (mstp section in bench.yml)")


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
        Dnsmasq(svc, rundir, dut_mac=bench.dut.mac, dut_ip=bench.dut.ip),
        Mosquitto(svc, rundir, BrokerTls(pki.ca, good.cert, good.key, crl=pki.crl)),
        Chrony(svc, rundir) if Chrony.available() else None,
    )
    with rig:
        yield rig


@pytest.fixture(scope="session")
def stim(bench: Bench, artifacts: Path) -> Iterator[Stim]:
    """The stimulus board (protocol v0), in the safe state before and after the session."""
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


# ---- per test -----------------------------------------------------------------------------
@pytest.fixture
def capture(
    request: pytest.FixtureRequest, netns: Topology, bench: Bench, artifacts: Path
) -> Iterator[Capture]:
    """A running capture on the DUT's wire (mark ``capture(bpf)`` narrows the filter).

    The pcapng is kept under ``<artifacts>/pcap``; with the ``services`` fixture active the
    broker's TLS key log is injected into it. A malformed frame from the DUT fails the test.
    """
    requires_tools("tshark", "editcap")
    marker = request.node.get_closest_marker("capture")
    bpf = marker.args[0] if marker else DEFAULT_BPF
    keylog = request.getfixturevalue("services").broker.keylog if "services" in request.fixturenames else None
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)
    cap = Capture(
        netns.capture_iface,
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

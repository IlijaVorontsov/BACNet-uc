"""``uc-hub demo``: the demo site, simulated, with a hub on it, in one process.

``hub/examples/demo`` holds the site (``site.yaml``), the hub configuration
(``hub.yaml``) and the app modules. The demo copies them into a working
directory (a fresh database every time), adapts the addresses and starts:

- a ``SimNode`` for every BACnet-uc node of the site, with a thermal room
  on its IO. All but ``r204-ctl`` are configured from the manifest by the
  hub's own plan and apply engine before the hub starts, so they already
  have their points and thermostats; ``r204-ctl`` stays empty, so
  commissioning room 204 makes a real plan;
- the simulated AHU controller (BACnet/IP) at the site's address; it is
  also on the simulated BACnet network, so r204's uc-link reaches it;
- mosquitto on a free port, with an ``apps/mqtt_tls`` node that behaves
  like MQTT firmware 0.3.0 and the JSON CO2 sensor of room 204 (without a
  ``mosquitto`` binary the site's MQTT devices are left out, with a warning);
- the hub, with the scripted model (``--llm zai``: Z.ai GLM with
  ``ZAI_API_KEY``) and the web app when ``web/dist`` is built.

By default the devices listen on the ports ``site.yaml`` names (SMP
13201-13205, the AHU on 47830); ``ephemeral_ports`` (``--free-ports``)
picks free ones, so several demos (the web app's end-to-end tests) can run
side by side.
"""

from __future__ import annotations

import asyncio
import copy
import getpass
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
import yaml

from .api import HubServer, create_app
from .core.types import ProtocolName
from .drivers.bacnet_uc import SmpNodeClient
from .manifest import MemoryBackupStore, SiteManifest, apply_plan, compute_plan, directory_resolver
from .manifest.load import dump_yaml, parse_yaml
from .runtime import Services, load_config
from .runtime.config import HubConfig
from .sim import RoomModel, SimNetwork, SimNode
from .sim.bacnet_ip_device import SimBacnetIpDevice
from .sim.mqtt_device import SimJsonSensor, SimMqttDevice, SimMqttTlsDevice, stop_sim_mqtt_devices

logger = logging.getLogger(__name__)

#: hub/examples/demo in a source checkout.
DEMO_DIR = Path(__file__).resolve().parents[2] / "examples" / "demo"
#: Nodes the demo leaves unconfigured, for the agent to commission.
EMPTY_NODES = frozenset({"r204-ctl"})
LOCALHOST = "127.0.0.1"


class Mosquitto:
    """A private mosquitto broker on a free port of 127.0.0.1 (plain MQTT)."""

    def __init__(self, workdir: Path, binary: str) -> None:
        self.workdir = workdir
        self.binary = binary
        self.port = 0
        self._proc: subprocess.Popen[bytes] | None = None

    @staticmethod
    def find() -> str | None:
        found = os.environ.get("MOSQUITTO") or shutil.which("mosquitto")
        if found is None and Path("/usr/sbin/mosquitto").exists():
            found = "/usr/sbin/mosquitto"
        return found

    async def start(self, timeout_s: float = 5.0) -> None:
        self.port = _free_port()
        conf = self.workdir / "mosquitto.conf"
        # "user": started as root, mosquitto would switch to its own user and lose the workdir.
        conf.write_text(f"listener {self.port} {LOCALHOST}\nallow_anonymous true\npersistence false\n"
                        f"log_dest stderr\nuser {getpass.getuser()}\n")
        with open(self.workdir / "mosquitto.log", "ab") as log:
            # Its own session: a Ctrl-C meant for the demo must not take the broker down before the hub.
            self._proc = subprocess.Popen([self.binary, "-c", str(conf)], stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                break
            try:
                _, writer = await asyncio.open_connection(LOCALHOST, self.port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            return
        self.stop()
        raise RuntimeError(f"mosquitto did not start; see {self.workdir / 'mosquitto.log'}")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


@dataclass
class Demo:
    workdir: Path
    config: HubConfig
    services: Services
    network: SimNetwork
    nodes: dict[str, SimNode]
    ahus: dict[str, SimBacnetIpDevice]
    mqtt: list[SimMqttDevice] = field(default_factory=list)
    broker: Mosquitto | None = None

    async def stop(self) -> None:
        """The hub first (it relinquishes and releases on the devices), then the devices."""
        try:
            await self.services.stop()
        finally:
            await stop_sim_mqtt_devices(self.mqtt)
            if self.broker is not None:
                self.broker.stop()
            for ahu in self.ahus.values():
                await ahu.stop()
            await self.network.aclose()


async def start_demo(
    workdir: Path,
    *,
    demo_dir: Path = DEMO_DIR,
    llm: str = "scripted",
    ephemeral_ports: bool = False,
    mqtt: bool = True,
    overrides: dict[str, Any] | None = None,
) -> Demo:
    """Start the simulated site and the hub on it; ``overrides`` are merged
    into hub.yaml (``listen``, faster driver settings in tests, ...)."""
    if not (demo_dir / "site.yaml").is_file():
        raise FileNotFoundError(f"{demo_dir} has no site.yaml; run the demo from a source checkout or pass --dir")
    workdir.mkdir(parents=True, exist_ok=True)
    site = parse_yaml((demo_dir / "site.yaml").read_text(encoding="utf-8"), "site.yaml")
    hub = parse_yaml((demo_dir / "hub.yaml").read_text(encoding="utf-8"), "hub.yaml") or {}
    network = SimNetwork()
    nodes: dict[str, SimNode] = {}
    ahus: dict[str, SimBacnetIpDevice] = {}
    devices: list[SimMqttDevice] = []
    broker: Mosquitto | None = None
    try:
        for entry in site["system"]["nodes"]:
            transport = entry["transport"]
            node = SimNode(name=entry["name"], instance=entry["device"]["instance"], host=LOCALHOST,
                           port=0 if ephemeral_ports else int(transport["port"]), network=network, room=RoomModel())
            await node.start()
            nodes[node.name] = node
            transport.update(host=LOCALHOST, port=node.port)
        for entry in _external(site, "bacnet-ip"):
            host, _, port = str(entry["address"]).partition(":")
            ahu = SimBacnetIpDevice(host=LOCALHOST, port=0 if ephemeral_ports else int(port or 47808),
                                    instance=entry["device_instance"], network=network)
            await ahu.start()
            ahus[entry["name"]] = ahu
            _rebind(site, entry["device_instance"], ahu.port)
            entry["address"] = ahu.address
        binary = Mosquitto.find() if mqtt and _external(site, "mqtt") else None
        if binary is not None:
            broker = Mosquitto(workdir, binary)
            await broker.start()
            devices = await _mqtt_devices(site, broker.port)
        elif _external(site, "mqtt"):
            logger.warning("the demo runs without MQTT devices: %s", "no mosquitto binary on PATH" if mqtt
                           else "MQTT is switched off")
            _drop_mqtt(site)
        _copy_site(demo_dir, workdir, site)
        (workdir / "hub.yaml").write_text(yaml.safe_dump(_hub_doc(hub, demo_dir, nodes, ahus, broker, llm),
                                                         sort_keys=False))
        config = load_config(workdir / "hub.yaml", overrides=overrides)
        await _provision(workdir, site, config, [n for n in nodes if n not in EMPTY_NODES])
        services = Services(config)
        await services.start()
    except BaseException:
        await stop_sim_mqtt_devices(devices)
        if broker is not None:
            broker.stop()
        for ahu in ahus.values():
            await ahu.stop()
        await network.aclose()
        raise
    return Demo(workdir, config, services, network, nodes, ahus, devices, broker)


def _external(site: dict[str, Any], protocol: str) -> list[dict[str, Any]]:
    return [d for d in site.get("external_devices") or [] if d.get("protocol") == protocol]


def _rebind(site: dict[str, Any], instance: int, port: int) -> None:
    """Static BACnet bindings of the nodes follow the device's port."""
    for node in site["system"]["nodes"]:
        for binding in (node.get("bacnet") or {}).get("static_bindings") or []:
            if binding.get("device") == instance:
                binding.update(address=LOCALHOST, port=port)


async def _mqtt_devices(site: dict[str, Any], port: int) -> list[SimMqttDevice]:
    broker = {"host": LOCALHOST, "port": port}
    devices: list[SimMqttDevice] = []
    for entry in _external(site, "mqtt"):
        if entry.get("profile", "mqtt_tls") == "mqtt_tls":
            devices.append(SimMqttTlsDevice(broker, entry["client_id"], topic_root=entry.get("topic_root", "bacnet-uc"),
                                            modern=True))
        else:
            devices.append(SimJsonSensor(broker, entry["topic"], interval_s=5.0))
    try:
        for device in devices:
            await device.start()
    except BaseException:
        await stop_sim_mqtt_devices(devices)
        raise
    return devices


def _drop_mqtt(site: dict[str, Any]) -> None:
    gone = {d["name"] for d in _external(site, "mqtt")}
    site["external_devices"] = [d for d in site.get("external_devices") or [] if d["name"] not in gone]
    site["placement"] = {k: v for k, v in (site.get("placement") or {}).items() if k not in gone}
    site["bridges"] = [b for b in site.get("bridges") or []
                       if not {_device(b["from"]), _device(b["to"])} & gone]
    for key in ("tags", "safety"):
        if key in site:
            site[key] = {k: v for k, v in site[key].items() if _device(k) not in gone}


def _device(point: str) -> str:
    parts = point.split("/")
    return parts[-2] if len(parts) >= 2 else point


def _copy_site(demo_dir: Path, workdir: Path, site: dict[str, Any]) -> None:
    (workdir / "site.yaml").write_text(dump_yaml(site))
    if (demo_dir / "apps").is_dir():
        shutil.copytree(demo_dir / "apps", workdir / "apps", dirs_exist_ok=True)


def _hub_doc(hub: dict[str, Any], demo_dir: Path, nodes: dict[str, SimNode], ahus: dict[str, SimBacnetIpDevice],
             broker: Mosquitto | None, llm: str) -> dict[str, Any]:
    """The demo's hub.yaml with what depends on the simulation filled in."""
    doc = copy.deepcopy(hub)
    doc["site_file"] = "site.yaml"
    doc["data_dir"] = "data"
    web = doc.pop("web_dir", None)
    if web is not None:
        web_dir = (demo_dir / web).resolve()
        if (web_dir / "index.html").is_file():
            doc["web_dir"] = str(web_dir)
        else:
            logger.warning("%s is not built (pnpm build in web/); the hub serves the API only", web_dir)
    drivers = doc.setdefault("drivers", {})
    drivers.setdefault("bacnet_uc", {})["discover_broadcast"] = [node.address for node in nodes.values()]
    if ahus:
        drivers.setdefault("bacnet_ip", {})["discover_targets"] = [ahu.address for ahu in ahus.values()]
    if broker is not None:
        drivers.setdefault("mqtt", {}).update(host=LOCALHOST, port=broker.port)
    else:
        drivers.pop("mqtt", None)
    if llm == "zai":
        doc["llm"] = {"provider": "zai", "model": "glm-5.3", "api_key_env": "ZAI_API_KEY", "reasoning_effort": "high"}
        if not os.environ.get("ZAI_API_KEY"):
            logger.warning("ZAI_API_KEY is not set: agent runs will fail until it is")
    elif llm != "scripted":
        raise ValueError(f"unknown model {llm!r} (scripted or zai)")
    return doc


class _NoGateway:
    """The gateway part is the hub's own job once it starts."""

    async def apply_bridges(self, bridges: list[dict[str, Any]]) -> None:
        return None

    async def apply_tags(self, tags: dict[str, list[str]], safety: dict[str, str]) -> None:
        return None


async def _provision(workdir: Path, site: dict[str, Any], config: HubConfig, names: list[str]) -> None:
    """Configure these nodes from the manifest, the way an applied plan
    would, and restart them, as an installer power-cycles a configured board."""
    manifest = SiteManifest.from_dict(site, workdir)
    settings = config.driver_settings(ProtocolName.BACNET_UC)
    clients: dict[str, SmpNodeClient] = {}

    def resolve(name: str) -> SmpNodeClient:
        if name not in clients:
            transport = manifest.nodes[name]["transport"]
            clients[name] = SmpNodeClient.from_settings(transport["host"], int(transport["port"]), settings,
                                                        name=name)
        return clients[name]

    try:
        plan = await compute_plan(manifest, None, resolve, directory_resolver(workdir), config.uc_link_wasm, 1, 0,
                                  plan_id="provision")
        plan.changes = [c for c in plan.changes if c.target in names]
        plan.blocked = {t: why for t, why in plan.blocked.items() if t in names}
        results = await apply_plan(plan, resolve, _NoGateway(), MemoryBackupStore(), None, len(names) or 1)
        failed = [f"{r.change_id}: {r.detail}" for r in results if not r.ok]
        if failed:
            raise RuntimeError("configuring the demo nodes failed: " + "; ".join(failed))
        await asyncio.gather(*(resolve(name).reset() for name in names))
    finally:
        for client in clients.values():
            await client.close()
    logger.info("demo: configured %s from the manifest; left empty: %s", ", ".join(names),
                ", ".join(sorted(EMPTY_NODES)))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOCALHOST, 0))
        return int(s.getsockname()[1])


async def run_demo(*, host: str = LOCALHOST, port: int = 8080, llm: str = "scripted",
                   demo_dir: Path = DEMO_DIR, free_ports: bool = False, token_env: str | None = None) -> None:
    """``uc-hub demo``: run until Ctrl-C (or SIGTERM). ``token_env`` names
    the environment variable of a bearer token for user ``demo`` (admin)
    instead of dev mode."""
    overrides: dict[str, Any] = {"listen": {"host": host, "port": port}}
    if token_env:
        if not os.environ.get(token_env, "").strip():
            raise ValueError(f"--token-env: the environment variable {token_env} is empty or not set")
        overrides["auth"] = {"tokens": [{"user": "demo", "roles": ["admin"], "token_env": token_env}]}
    with tempfile.TemporaryDirectory(prefix="uc-hub-demo-") as tmp:
        demo = await start_demo(Path(tmp), demo_dir=demo_dir, llm=llm, ephemeral_ports=free_ports,
                                overrides=overrides)
        try:
            app = create_app(demo.services)
            server = HubServer(uvicorn.Config(app, host=host, port=port, log_config=None, lifespan="off",
                                              timeout_graceful_shutdown=2))
            serving = asyncio.create_task(server.serve())
            while not server.started and not serving.done():
                await asyncio.sleep(0.05)
            if server.started:
                sign_in = f"; sign in once with /?token=<${token_env}>" if token_env else ""
                print(f"uc-hub demo: http://{host}:{port}/ (site {demo.services.site.name}, working directory {tmp})"
                      f"{sign_in}; try 'Commission room 204' in the agent panel. Ctrl-C stops.", flush=True)
            await serving
        finally:
            await demo.stop()

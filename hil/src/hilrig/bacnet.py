"""bacnet-stack command-line tools run inside a rig namespace, with parsed results.

The tools (bacwi, bacrp, bacwp, bacrpm, bacrfdt, bacrbdt, bacepics, bacserv, ...) are
configured through environment variables (BACNET_IFACE, BACNET_BBMD_ADDRESS, ...). They are
taken from ``$HIL_BACNET_BIN`` or, by default, ``hil/tools/bacnet-stack/bin``, built from
bacnet-stack at the firmware's SHA with ``BACDL=bip BBMD=full`` (see hil/README.md).
The output formats parsed here are those of bacnet-stack 1.7 (54544d02).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from hilrig import netns as nsmod

TOOLS_ENV = "HIL_BACNET_BIN"
DEFAULT_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "bacnet-stack" / "bin"
BACNET_PORT = 47808

ObjectId = tuple[str, int]

_ERROR_RE = re.compile(r"BACnet (Error|Reject|Abort): (.*)")
_WHOIS_RE = re.compile(r"^\s*(\d+)\s+([0-9A-Fa-f:]+)\s+(\d+)\s+([0-9A-Fa-f:]+)\s+(\d+)\s*$")
_TABLE_RE = re.compile(r"^(FDT|BDT)-\d+: (\S+):(\d+) (\S+)(?: (\d+)s)?")
_RPM_OBJECT_RE = re.compile(r"^([a-z-]+) #(\d+)$")
_OBJECT_ID_RE = re.compile(r"\(([a-z-]+), (\d+)\)")


def tools_dir() -> Path:
    """Return the directory of the bacnet-stack tools (``$HIL_BACNET_BIN`` or the default)."""
    return Path(os.environ.get(TOOLS_ENV, DEFAULT_TOOLS))


def tool_env(settings: Mapping[str, str]) -> dict[str, str]:
    """Return this process's environment without its BACNET_* variables, plus ``settings``.

    The tools take their whole configuration from BACNET_* variables, so one left in the
    developer's shell (BACNET_IP_PORT, BACNET_BBMD_ADDRESS, ...) would change every tool.
    """
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("BACNET_")}
    return {**inherited, **settings}


class BacnetError(RuntimeError):
    """A tool reported a BACnet Error, Reject or Abort, or the device did not answer.

    ``kind`` is ``error``, ``reject``, ``abort`` or ``timeout``. For errors, ``error_class``
    and ``error_code`` hold the names the tool printed (``object``, ``unknown-object``).
    """

    def __init__(self, tool: str, kind: str, detail: str) -> None:
        super().__init__(f"{tool}: {kind}: {detail}")
        self.kind = kind
        self.detail = detail
        cls, _, code = detail.partition(": ")
        self.error_class = cls if kind == "error" else ""
        self.error_code = code if kind == "error" else ""


@dataclass(frozen=True)
class Device:
    """One line of bacwi output: a device that answered Who-Is with I-Am."""

    instance: int
    mac: str
    snet: int
    sadr: str
    max_apdu: int

    @property
    def address(self) -> tuple[str, int]:
        """Return (IP, UDP port) for a BACnet/IP MAC (6 octets); ValueError otherwise."""
        octets = bytes.fromhex(self.mac.replace(":", ""))
        if len(octets) != 6:
            raise ValueError(f"{self.mac} is not a BACnet/IP address")
        return ".".join(str(b) for b in octets[:4]), int.from_bytes(octets[4:], "big")


@dataclass(frozen=True)
class FdtEntry:
    """One Foreign-Device-Table entry from bacrfdt: address, TTL and time remaining."""

    ip: str
    port: int
    ttl_s: int
    remaining_s: int


@dataclass(frozen=True)
class BdtEntry:
    """One Broadcast-Distribution-Table entry from bacrbdt."""

    ip: str
    port: int
    mask: str


@dataclass(frozen=True)
class Epics:
    """The parts of a bacepics report the tests use: services and object properties.

    Property values are the text bacepics prints; ``?`` means the tool did not read the
    value, and a trailing ``Writable`` marker is removed (listed in ``writable``).
    """

    services: tuple[str, ...]
    objects: Mapping[ObjectId, Mapping[str, str]]
    writable: frozenset[tuple[ObjectId, str]]


def parse_whois(text: str) -> list[Device]:
    """Parse the device table that bacwi prints."""
    devices = []
    for line in text.splitlines():
        m = _WHOIS_RE.match(line)
        if m:
            devices.append(Device(int(m[1]), m[2].upper(), int(m[3]), m[4].upper(), int(m[5])))
    return devices


def parse_fdt(text: str) -> list[FdtEntry]:
    """Parse bacrfdt output (``FDT-001: 198.51.100.10:47808 60s 88s``)."""
    entries = []
    for line in text.splitlines():
        m = _TABLE_RE.match(line)
        if m and m[1] == "FDT":
            entries.append(FdtEntry(m[2], int(m[3]), int(m[4].rstrip("s")), int(m[5])))
    return entries


def parse_bdt(text: str) -> list[BdtEntry]:
    """Parse bacrbdt output (``BDT-001: 192.0.2.10:47808 255.255.255.255``)."""
    return [
        BdtEntry(m[2], int(m[3]), m[4])
        for m in map(_TABLE_RE.match, text.splitlines())
        if m and m[1] == "BDT"
    ]


def parse_rpm(text: str) -> dict[ObjectId, dict[str, str]]:
    """Parse bacrpm output into {(object-type, instance): {property: value}}.

    A property the device refused keeps the tool's text as its value, for example
    ``BACnet Error: property: unknown-property``.
    """
    result: dict[ObjectId, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        m = _RPM_OBJECT_RE.match(line.strip())
        if m:
            current = result.setdefault((m[1], int(m[2])), {})
        elif current is not None and ": " in line and line.startswith("    "):
            prop, _, value = line.strip().partition(": ")
            current[prop] = value
    return result


def _strip_comment(line: str) -> str:
    """Drop an EPICS ``-- comment`` from ``line``; ``--`` inside a quoted string is text."""
    quoted = False
    for i, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif not quoted and line.startswith("--", i):
            return line[:i].rstrip()
    return line.rstrip()


def parse_epics(text: str) -> Epics:
    """Parse a bacepics report (services supported and the list of objects).

    In the object list, a property line is indented by 4 spaces and a continuation of a
    multi-line value (arrays, bit strings) by more.
    """
    services: list[str] = []
    objects: dict[ObjectId, dict[str, str]] = {}
    writable: set[tuple[ObjectId, str]] = set()
    section = ""
    obj: ObjectId | None = None
    prop = ""
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if raw.startswith("BACnet Standard Application Services Supported:"):
            section = "services"
        elif raw.startswith("List of Objects in Test Device:"):
            section = "objects"
        elif section == "services":
            if line.strip() == "}":
                section = ""
            elif line.strip() not in ("", "{"):
                services.append(line.strip())
        elif section == "objects" and line.startswith("    ") and not line.startswith("     "):
            prop, _, value = line.strip().partition(": ")
            if prop == "object-identifier":
                m = _OBJECT_ID_RE.match(value)
                obj = (m[1], int(m[2])) if m else None
            if obj is None:
                continue
            if value.endswith(" Writable"):
                value = value[: -len(" Writable")]
                writable.add((obj, prop))
            objects.setdefault(obj, {})[prop] = value
        elif section == "objects" and obj is not None and line.startswith("     "):
            objects[obj][prop] += " " + line.strip()
    return Epics(tuple(services), objects, frozenset(writable))


def _failure(tool: str, proc: subprocess.CompletedProcess[str]) -> BacnetError | None:
    out = proc.stdout + proc.stderr
    m = _ERROR_RE.search(out)
    if m:
        return BacnetError(tool, m[1].lower(), m[2].strip())
    if "APDU Timeout" in out or proc.returncode != 0:
        return BacnetError(tool, "timeout", out.strip() or f"exit status {proc.returncode}")
    return None


class Bacnet:
    """bacnet-stack client tools bound to one namespace and interface.

    Tools bind to the target device with Who-Is/I-Am, so a device behind a BBMD needs a
    client registered as a foreign device: see :meth:`foreign`.
    """

    def __init__(
        self,
        netns: str | None,
        iface: str,
        *,
        tools: Path | None = None,
        env: Mapping[str, str] | None = None,
        apdu_timeout_ms: int = 2000,
        apdu_retries: int = 1,
    ) -> None:
        self.netns = netns
        self.iface = iface
        self.tools = tools or tools_dir()
        self.env = {
            "BACNET_IFACE": iface,
            "BACNET_APDU_TIMEOUT": str(apdu_timeout_ms),
            "BACNET_APDU_RETRIES": str(apdu_retries),
            **(env or {}),
        }

    def with_env(self, **env: str) -> Bacnet:
        """Return a copy with extra tool environment variables."""
        return Bacnet(self.netns, self.iface, tools=self.tools, env={**self.env, **env})

    def foreign(self, bbmd_ip: str, ttl_s: int = 60) -> Bacnet:
        """Return a copy whose tools register as a foreign device with ``bbmd_ip`` first."""
        return self.with_env(BACNET_BBMD_ADDRESS=bbmd_ip, BACNET_BBMD_TIMETOLIVE=str(ttl_s))

    def run(self, tool: str, *args: object, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        """Run one tool and return its output; the exit status is not checked."""
        path = self.tools / tool
        if not path.exists():
            raise FileNotFoundError(f"{path} not found (set {TOOLS_ENV}, see hil/README.md)")
        return nsmod.run(
            self.netns,
            [str(path), *map(str, args)],
            env=tool_env(self.env),
            timeout=timeout,
            check=False,
            cwd=Path("/"),
        )

    def _checked(self, tool: str, *args: object) -> str:
        proc = self.run(tool, *args)
        failure = _failure(tool, proc)
        if failure:
            raise failure
        return proc.stdout

    def whois(self, low: int | None = None, high: int | None = None, *, wait_ms: int = 1000) -> list[Device]:
        """Send Who-Is (optionally limited to low..high) and return the devices that answered."""
        limits = [] if low is None else [low, low if high is None else high]
        return parse_whois(self.run("bacwi", "--timeout", wait_ms, *limits).stdout)

    def read(self, device: int, obj_type: str, instance: int, prop: str, index: int | None = None) -> str:
        """ReadProperty; return the value as bacrp prints it (strings keep their quotes)."""
        extra = [] if index is None else [index]
        return self._checked("bacrp", device, obj_type, instance, prop, *extra).strip()

    def read_multiple(
        self, device: int, request: Mapping[ObjectId, Sequence[str]]
    ) -> dict[ObjectId, dict[str, str]]:
        """ReadPropertyMultiple for {(type, instance): [properties]}; see :func:`parse_rpm`."""
        args: list[object] = []
        for (obj_type, instance), props in request.items():
            args += [obj_type, instance, ",".join(props)]
        proc = self.run("bacrpm", device, *args)
        values = parse_rpm(proc.stdout)
        failure = None if values else _failure("bacrpm", proc)
        if failure:
            raise failure
        return values

    def write(
        self,
        device: int,
        obj_type: str,
        instance: int,
        prop: str,
        value: object,
        *,
        tag: int,
        priority: int = 16,
        index: int = -1,
    ) -> None:
        """WriteProperty with an application ``tag`` (4 = REAL, 2 = Unsigned, 9 = Enumerated)."""
        self._checked("bacwp", device, obj_type, instance, prop, priority, index, tag, value)

    def read_fdt(self, bbmd_ip: str) -> list[FdtEntry]:
        """Read-Foreign-Device-Table from a BBMD (empty if it does not answer)."""
        return parse_fdt(self.run("bacrfdt", bbmd_ip).stdout)

    def read_bdt(self, bbmd_ip: str) -> list[BdtEntry]:
        """Read-Broadcast-Distribution-Table from a BBMD (empty if it does not answer)."""
        return parse_bdt(self.run("bacrbdt", bbmd_ip).stdout)

    def epics(self, device: int) -> Epics:
        """Generate and parse the EPICS of a device with bacepics.

        Properties the device refuses to read appear as ``?`` (bacepics comments the error).
        """
        proc = self.run("bacepics", device, timeout=120.0)
        epics = parse_epics(proc.stdout)
        failure = None if epics.objects else _failure("bacepics", proc)
        if failure:
            raise failure
        return epics


class BacServ:
    """A bacserv process in a namespace: a simulated device, or the SIL stand-in DUT.

    The stock bacserv (built with BBMD=full) is also a BBMD with itself as BDT entry 1,
    so it accepts foreign-device registrations.
    """

    def __init__(
        self,
        netns: str | None,
        iface: str,
        instance: int,
        name: str,
        *,
        log: Path,
        tools: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.netns, self.instance, self.name = netns, instance, name
        self.argv = [str((tools or tools_dir()) / "bacserv"), str(instance), name]
        self.env = {"BACNET_IFACE": iface, **(env or {})}
        self.log = log
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self, timeout: float = 5.0) -> BacServ:
        """Start bacserv and wait until it has bound UDP 47808."""
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.proc = nsmod.spawn(
            self.netns, self.argv, log=self.log, env=tool_env(self.env), cwd=self.log.parent
        )
        deadline = time.monotonic() + timeout
        while not nsmod.listening(self.netns, "udp", BACNET_PORT):
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError(f"bacserv did not bind UDP {BACNET_PORT}; see {self.log}")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        """Stop bacserv (SIGTERM, then SIGKILL after 5 s) and close its stdin pipe."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.stdin:
            proc.stdin.close()

    def __enter__(self) -> BacServ:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

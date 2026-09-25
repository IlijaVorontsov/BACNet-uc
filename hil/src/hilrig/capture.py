"""Per-test packet capture on the wire interface, analysed with tshark after it stops.

Rules (design section 3.1):

- one pcapng per test, written by tshark/dumpcap running inside the capture namespace, with
  a narrow BPF filter;
- a readiness barrier: UDP/9 sentinels are broadcast until tshark reports one, because
  "Capturing on ..." is printed before the socket really receives; sentinels are excluded
  from every analysis;
- analysis only after :meth:`Capture.stop`, with ``tshark -r ... -Y ... -T fields``;
- with TLS key logs (the broker's, and those of the TLS endpoints a test adds with
  :meth:`Capture.add_keylog`), the secrets are merged and injected into the pcapng
  (``editcap --inject-secrets``) so the artifact decrypts on its own;
- dumpcap's drop count is reported by :meth:`Capture.dropped` (R-04 needs 0).

The sentinel needs carrier on the capture interface: a DUT that is powered off cannot be
the only thing behind it when the capture starts.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hilrig import netns as nsmod

SENTINEL_PORT = 9
SENTINEL_PAYLOAD = b"hil-capture-sentinel"
NOT_SENTINEL = f"!(udp.dstport == {SENTINEL_PORT})"
STOP_TIMEOUT_S = 10.0  # for tshark to finish the file after SIGINT, before SIGKILL


class CaptureError(RuntimeError):
    """The capture did not become ready, or tshark/editcap failed."""


@dataclass(frozen=True)
class Row:
    """One packet from :meth:`Capture.rows`: capture time (epoch s) and the requested fields.

    Missing fields are empty strings. ``row["ip.src"]`` reads a field.
    """

    t: float
    fields: Mapping[str, str]

    def __getitem__(self, name: str) -> str:
        return self.fields[name]


class Capture:
    """A capture of ``iface`` inside ``netns`` into ``out`` (pcapng), as a context manager.

    ``sentinel_netns`` / ``sentinel_dst`` say where the readiness sentinels come from and
    go to; the default broadcasts from netns svc into subnet A, which floods every port of
    br-a and needs no ARP. ``keylog`` is an NSS key log to inject after the capture stops.
    """

    def __init__(
        self,
        iface: str,
        netns: str | None,
        bpf: str,
        out: Path,
        *,
        sentinel_netns: str | None = "svc",
        sentinel_dst: str = nsmod.BROADCAST_A,
        keylog: Path | None = None,
        ready_timeout: float = 10.0,
        settle: float = 0.3,
    ) -> None:
        self.iface = iface
        self.netns = netns
        self.bpf = bpf
        self.path = out
        self.sentinel_netns = sentinel_netns
        self.sentinel_dst = sentinel_dst
        self.keylogs: list[Path] = [keylog] if keylog else []
        self.ready_timeout = ready_timeout
        self.settle = settle
        self.log = out.with_suffix(".tshark.log")
        self._proc: subprocess.Popen[str] | None = None
        self._ready = threading.Event()
        self._reader: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Tell whether the capture is running."""
        return self._proc is not None

    @property
    def keylog(self) -> Path | None:
        """The first key log (the broker's, when the ``services`` fixture is active)."""
        return self.keylogs[0] if self.keylogs else None

    def add_keylog(self, path: Path) -> None:
        """Inject this key log too when the capture stops (s_server, the TLS front)."""
        if path not in self.keylogs:
            self.keylogs.append(path)

    def __enter__(self) -> Capture:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> Capture:
        """Start tshark and return once a sentinel has been captured."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        capture_filter = f"udp dst port {SENTINEL_PORT}"
        if self.bpf:
            capture_filter = f"({self.bpf}) or ({capture_filter})"
        # -P prints one line per packet (its UDP destination port) while writing the file.
        tshark = ["tshark", "-n", "-l", "-P", "-i", self.iface, "-f", capture_filter, "-w", str(self.path)]
        argv = nsmod.exec_argv(self.netns, [*tshark, "-T", "fields", "-e", "udp.dstport"])
        # Own process group: tshark forks dumpcap, and stopping must reach both (see _terminate).
        with self.log.open("w") as err:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=err, text=True, start_new_session=True
            )
        self._ready.clear()
        self._reader = threading.Thread(target=self._watch, args=(self._proc,), daemon=True)
        self._reader.start()
        try:
            self._barrier()
        except BaseException:
            self._terminate()
            raise
        return self

    def _watch(self, proc: subprocess.Popen[str]) -> None:
        """Read tshark's per-packet lines (so the pipe never fills) and spot a sentinel."""
        assert proc.stdout is not None
        with proc.stdout:
            for line in proc.stdout:
                if line.strip() == str(SENTINEL_PORT):
                    self._ready.set()

    def _barrier(self) -> None:
        with nsmod.enter(self.sentinel_netns) if self.sentinel_netns else contextlib.nullcontext():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            deadline = time.monotonic() + self.ready_timeout
            while not self._ready.wait(0.1):
                if self._proc is None or self._proc.poll() is not None or time.monotonic() > deadline:
                    raise CaptureError(
                        f"capture on {self.iface} ({self.netns}) never saw a sentinel; "
                        f"see {self.log}: {self.log.read_text()[-500:]!r}"
                    )
                sock.sendto(SENTINEL_PAYLOAD, (self.sentinel_dst, SENTINEL_PORT))

    def stop(self) -> Path:
        """Stop the capture (idempotent), inject the key log if any, return the pcapng."""
        if self._proc is None:
            return self.path
        time.sleep(self.settle)  # frames still in flight land in the file
        self._terminate()
        secrets = b"".join(k.read_bytes() for k in self.keylogs if k.exists())
        if secrets.strip():
            merged = self.path.with_suffix(".keys")
            merged.write_bytes(secrets if secrets.endswith(b"\n") else secrets + b"\n")
            injected = self.path.with_suffix(".dsb.pcapng")
            _tool(["editcap", "--inject-secrets", f"tls,{merged}", str(self.path), str(injected)])
            os.replace(injected, self.path)
        return self.path

    def texts(self, display_filter: str, proto: str = "bacapp") -> list[TextRow]:
        """:func:`texts` of this (stopped) capture."""
        if self.running:
            raise CaptureError("texts() analyses a stopped capture; call stop() first")
        return texts(self.path, display_filter, proto)

    def dropped(self) -> int | None:
        """Packets the kernel or dumpcap dropped, from tshark's summary (None: no summary yet)."""
        text = self.log.read_text() if self.log.exists() else ""
        if not re.search(r"\b\d+ packets? captured", text):
            return None
        return sum(int(n) for n in re.findall(r"\b(\d+) packets? dropped", text))

    def _terminate(self) -> None:
        """Stop tshark and its dumpcap child, as Ctrl-C would, and kill both if that hangs.

        Signalling tshark alone could leave dumpcap capturing when tshark has to be killed.
        """
        proc, self._proc = self._proc, None
        if proc is None:
            return
        _signal_group(proc, signal.SIGINT)
        try:
            proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            proc.wait()
        if self._reader:
            self._reader.join(timeout=5)

    def rows(self, display_filter: str, *fields: str, all_occurrences: bool = False) -> list[Row]:
        """Return the packets matching ``display_filter``, sentinels excluded (see :func:`rows_of`)."""
        if self.running:
            raise CaptureError("rows() analyses a stopped capture; call stop() first")
        return rows_of(self.path, display_filter, *fields, all_occurrences=all_occurrences)


def rows_of(path: Path, display_filter: str, *fields: str, all_occurrences: bool = False) -> list[Row]:
    """The packets of the pcapng ``path`` matching ``display_filter``, sentinels excluded.

    Each row carries ``frame.time_epoch`` and the first occurrence of every field (all
    occurrences, comma-separated, with ``all_occurrences``).
    """
    occurrence = "a" if all_occurrences else "f"
    output = ["-T", "fields", "-E", "separator=\t", "-E", f"occurrence={occurrence}", "-E", "aggregator=,"]
    argv = ["tshark", "-n", "-r", str(path), "-Y", f"({display_filter}) && {NOT_SENTINEL}", *output]
    for field in ("frame.time_epoch", *fields):
        argv += ["-e", field]
    rows = []
    # tshark escapes tab and newline inside values but leaves other characters that
    # str.splitlines() would split on (\x1c, U+0085, U+2028), so split on "\n" only.
    for line in filter(None, _tool(argv).split("\n")):
        time_epoch, *values = line.split("\t")  # every field has a column, empty if absent
        rows.append(Row(float(time_epoch), dict(zip(fields, values, strict=True))))
    return rows


@dataclass(frozen=True)
class TextRow:
    """One packet from :meth:`Capture.texts`: capture time and the protocol's text items."""

    t: float
    texts: tuple[str, ...]

    def value(self, label: str) -> str | None:
        """The text after ``<label>:`` of the first item with that label, else None."""
        for text in self.texts:
            name, sep, value = text.partition(":")
            if sep and name.strip() == label:
                return value.strip()
        return None


def texts(path: Path, display_filter: str, proto: str = "bacapp") -> list[TextRow]:
    """The unnamed text items of ``proto`` in each matching packet (sentinels excluded).

    Some dissectors show decoded values only as text items, which ``-T fields`` cannot
    extract: bacapp prints ``Maximum ADPU Length Accepted: (Unsigned) 1476`` or
    ``firmware-revision: UTF-8 '0.1.0'``. These come from ``tshark -T pdml``.
    """
    pdml = _tool(
        ["tshark", "-n", "-r", str(path), "-Y", f"({display_filter}) && {NOT_SENTINEL}", "-T", "pdml"]
    )
    rows = []
    for packet in ET.fromstring(pdml).iter("packet"):
        epoch = packet.find("./proto[@name='frame']/field[@name='frame.time_epoch']")
        items = [
            f.get("show", "")
            for p in packet.iter("proto")
            if p.get("name") == proto
            for f in p.iter("field")
            if f.get("name") == "" and f.get("show")
        ]
        rows.append(TextRow(float(epoch.get("show", "0")) if epoch is not None else 0.0, tuple(items)))
    return rows


def _signal_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Send ``sig`` to the process group that ``proc`` leads (gone already: nothing to do)."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, sig)


def _tool(argv: list[str]) -> str:
    proc = subprocess.run(argv, text=True, capture_output=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise CaptureError(f"{argv[0]} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def connection_attempts(rows: list[Row]) -> list[float]:
    """First-SYN times per TCP source port: one connect(), however often its SYN was resent.

    ``rows`` are SYN (no ACK) packets with the ``tcp.srcport`` field.
    """
    first: dict[str, float] = {}
    for row in rows:
        first.setdefault(row["tcp.srcport"], row.t)
    return sorted(first.values())


def sentinel_count(path: Path) -> int:
    """Return how many readiness sentinels a capture file holds (for rig self-tests)."""
    query = ["-Y", f"udp.dstport == {SENTINEL_PORT}", "-T", "fields", "-e", "frame.number"]
    return len(_tool(["tshark", "-n", "-r", str(path), *query]).splitlines())

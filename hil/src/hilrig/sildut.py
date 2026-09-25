"""A native_sim DUT for the SIL tier: ``zephyr.exe`` inside netns lan-a on TAP ``zeth``.

``hil-net-up --sil`` creates the persistent TAP ``zeth`` on br-a in lan-a; a native_sim
build with the TAP Ethernet driver (``CONFIG_ETH_NATIVE_TAP``, FW-13) started inside lan-a
attaches to it, so the DUT is on subnet A like the real board: DHCP from dnsmasq,
``broker.hil.lan`` from its DNS, BACnet/IP broadcasts on br-a, captures on ``zeth``.

The process gets the options its build offers (read from ``zephyr.exe --help``):
``--mac-addr`` (needs ``CONFIG_ETH_NATIVE_TAP_RANDOM_MAC=n``) so the bench's DHCP
reservation matches, ``--device_id`` so the hwinfo UID and the MQTT client id are known,
``--flash`` so the flash simulator's file (``/lfs``) lives in the run directory and
survives restarts like the NOR does, and ``--uart_stdinout`` so the console UART is the
process's stdio (the DUT console) instead of a pty.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from hilrig import netns as nsmod
from hilrig.console import ProcessConsole

BOOT_BANNER = r"\*\*\* Booting Zephyr OS"
SIL_MAC = "02:48:49:4c:00:0a"  # locally administered; the SIL bench's DHCP reservation
SIL_DEVICE_ID = 0x48494C31  # "HIL1": hwinfo UID 48494c31, MQTT client id z914koc8


class SilDutError(RuntimeError):
    """zephyr.exe did not start or exited."""


def supported_options(exe: Path) -> set[str]:
    """The ``--option`` names ``exe --help`` lists (native_sim's command line parser)."""
    proc = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=30, check=False)
    text = proc.stdout + proc.stderr
    return {w.lstrip("[-").split("=")[0].rstrip("]") for w in text.split() if w.startswith(("-", "[-"))}


class SilDut:
    """One native_sim DUT process; :meth:`restart` is its reset and power cycle."""

    def __init__(
        self,
        exe: Path,
        rundir: Path,
        *,
        netns: str | None,
        mac: str | None = SIL_MAC,
        device_id: int | None = SIL_DEVICE_ID,
        extra_args: Sequence[str] = (),
    ) -> None:
        self.exe, self.rundir, self.netns = exe, rundir, netns
        self.mac, self.device_id, self.extra_args = mac, device_id, list(extra_args)
        self.console = ProcessConsole(rundir / "console.log", writable=True)
        self.proc: subprocess.Popen[bytes] | None = None
        self.starts = 0
        self.started_at = 0.0  # time.time() of the last start: "reset release" in SIL
        self.exits = 0  # exits of its own (sys_reboot or a fatal error), each followed by a restart
        self._stopping = False
        self._watch: threading.Thread | None = None

    def argv(self) -> list[str]:
        """The command line, with only the options this build supports."""
        options = supported_options(self.exe)
        argv = [str(self.exe)]
        if self.mac and "mac-addr" in options:
            argv.append(f"--mac-addr={self.mac}")
        if self.device_id is not None and "device_id" in options:
            argv.append(f"--device_id={self.device_id}")
        if "flash" in options:
            argv.append(f"--flash={self.rundir / 'flash.bin'}")
        if "uart_stdinout" in options:  # the console UART on stdio, not a pty
            argv.append("--uart_stdinout")
        return argv + self.extra_args

    def start(self) -> SilDut:
        """Start zephyr.exe inside the namespace and attach its output to :attr:`console`."""
        self.rundir.mkdir(parents=True, exist_ok=True)
        argv = nsmod.exec_argv(self.netns, self.argv())
        self._stopping = False
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        since = self.console.mark()
        self.console.attach(self.proc)
        self.starts += 1
        self.started_at = time.time()
        with contextlib.suppress(TimeoutError):  # booted: later banners mean reboots (dutctl)
            self.console.wait_for(BOOT_BANNER, 5.0, since)
        if self.proc.poll() is not None:
            raise SilDutError(f"{self.exe} exited with {self.proc.returncode}; see {self.console.log}")
        threading.Thread(target=self._restart_on_exit, args=(self.proc,), daemon=True).start()
        return self

    def _restart_on_exit(self, proc: subprocess.Popen[bytes]) -> None:
        """Start the process again when it ends by itself, as hardware comes back.

        native_sim exits on sys_reboot unless built with CONFIG_NATIVE_SIM_REBOOT (which
        re-executes in place), and on a fatal error.
        """
        proc.wait()
        if self._stopping or proc is not self.proc:
            return
        self.exits += 1
        self.console.close()
        time.sleep(0.2)
        if not self._stopping:
            self.start()

    @property
    def generation(self) -> int:
        """Boots so far (Zephyr boot banners): changes on every start and in-place reboot."""
        return len(self.console.lines(0, BOOT_BANNER))

    def running(self) -> bool:
        """Tell whether the process is alive (native_sim exits on sys_reboot or a fatal error)."""
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        """Stop the process: SIGTERM, then SIGKILL after 5 s (idempotent)."""
        self._stopping = True
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
        self.console.close()
        if proc.stdin:
            proc.stdin.close()

    def restart(self, off_s: float = 0.2) -> float:
        """Stop, wait ``off_s``, start again; returns the start time (``time.time()``)."""
        self.stop()
        time.sleep(off_s)
        self.start()
        return self.started_at

    def __enter__(self) -> SilDut:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
        self.console.shutdown()

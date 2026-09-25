"""Running external programs (DESIGN.md §19.2, §19.4).

Every external command of uc-ctl goes through a Runner, so tests can replace
it with FakeRunner and assert on the recorded argv lists.

``Runner(no_systemd=True)`` turns every ``systemctl`` call into a no-op that is
logged and recorded in ``runner.deferred`` (containers without systemd, §5.4).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

log = logging.getLogger(__name__)

# /usr/sbin is not on PATH for every caller, but most of our tools live there.
EXTRA_PATH = ("/usr/sbin", "/sbin", "/usr/local/sbin")


class CommandError(subprocess.CalledProcessError):
    """A command exited with a code it was not allowed to."""

    def __str__(self) -> str:
        tail = (self.stderr or self.output or "").strip()
        if isinstance(tail, bytes):
            tail = tail.decode(errors="replace")
        msg = f"{' '.join(map(str, self.cmd))} exited with {self.returncode}"
        return f"{msg}: {tail[-500:]}" if tail else msg


def _completed(argv, rc=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


class Runner:
    """Runs commands for real."""

    def __init__(self, no_systemd: bool = False):
        self.no_systemd = no_systemd
        # Human-readable commands that were skipped because of no_systemd.
        self.deferred: list[str] = []

    # -- plumbing ---------------------------------------------------------

    def which(self, name: str) -> str | None:
        """Find an executable on PATH, then in the sbin directories."""
        if os.path.isabs(name):
            return name if os.access(name, os.X_OK) else None
        found = shutil.which(name)
        if found:
            return found
        return shutil.which(name, path=os.pathsep.join(EXTRA_PATH))

    def _exec(self, argv, input, timeout, env, cwd, binary) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        exe = self.which(argv[0])
        if exe is None:
            return _completed(argv, 127, b"" if binary else "", f"{argv[0]}: command not found")
        data = input
        if data is not None and not binary and isinstance(data, str):
            data = data.encode()
        try:
            proc = subprocess.run([exe, *argv[1:]], input=data, capture_output=True,
                                  timeout=timeout, env=env, cwd=cwd, check=False)
        except FileNotFoundError:
            return _completed(argv, 127, b"" if binary else "", f"{argv[0]}: command not found")
        out, err = proc.stdout, proc.stderr
        if not binary:
            out = out.decode(errors="replace")
            err = err.decode(errors="replace")
        return _completed(argv, proc.returncode, out, err)

    # -- public API ------------------------------------------------------------

    def run(self, argv: Sequence[str], check: bool = True, input: str | bytes | None = None,
            timeout: float | None = None, env: dict | None = None, cwd: str | None = None,
            binary: bool = False) -> subprocess.CompletedProcess:
        """Run argv and capture its output (str, or bytes with binary=True).

        A missing program gives returncode 127. With check=True a non-zero
        exit raises CommandError. A timeout raises subprocess.TimeoutExpired.
        """
        log.debug("run: %s", " ".join(map(str, argv)))
        res = self._exec(argv, input, timeout, env, cwd, binary)
        if check and res.returncode != 0:
            raise CommandError(res.returncode, res.args, res.stdout, res.stderr)
        return res

    def systemctl(self, action: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        """``systemctl ACTION ARGS...``; a logged no-op with no_systemd."""
        argv = ["systemctl", action, *args]
        if self.no_systemd:
            cmd = " ".join(argv)
            log.info("no-systemd: skipped %s", cmd)
            self.deferred.append(cmd)
            return _completed(argv)
        return self.run(argv, check=check)

    def as_user(self, user: str, argv: Sequence[str], **kw) -> subprocess.CompletedProcess:
        """Run argv as another user (runuser, util-linux)."""
        return self.run(["runuser", "-u", user, "--", *argv], **kw)


Result = subprocess.CompletedProcess | Callable[[list[str], object], subprocess.CompletedProcess]


@dataclass
class _Script:
    prefix: tuple[str, ...]
    result: Result


class FakeRunner(Runner):
    """Records commands instead of running them.

    ``calls`` holds every argv list (systemctl calls included, unless
    no_systemd skipped them). Results are scripted per argv prefix; the
    longest matching prefix wins; the default is rc 0 with empty output::

        r = FakeRunner()
        r.script(["getent", "group", "ssh-admins"], rc=2)
        r.script(["nft", "list"], stdout="table inet uc {}")
        r.script(["chronyc"], result=lambda argv, input: ...)

    ``missing`` lists executables that which() reports as absent.
    """

    def __init__(self, no_systemd: bool = False, missing: Sequence[str] = ()):
        super().__init__(no_systemd=no_systemd)
        self.calls: list[list[str]] = []
        self.inputs: list[object] = []
        self.missing = set(missing)
        self._scripts: list[_Script] = []

    def script(self, prefix: Sequence[str], rc: int = 0, stdout: str = "", stderr: str = "",
               result: Result | None = None) -> None:
        res = result if result is not None else _completed(prefix, rc, stdout, stderr)
        self._scripts.append(_Script(tuple(prefix), res))

    def which(self, name: str) -> str | None:
        base = os.path.basename(name)
        if name in self.missing or base in self.missing:
            return None
        return name if os.path.isabs(name) else "/usr/bin/" + name

    def _exec(self, argv, input, timeout, env, cwd, binary) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.inputs.append(input)
        if self.which(argv[0]) is None:
            return _completed(argv, 127, "", f"{argv[0]}: command not found")
        best = None
        for s in self._scripts:
            if tuple(argv[: len(s.prefix)]) == s.prefix and (best is None or len(s.prefix) >= len(best.prefix)):
                best = s
        if best is None:
            return _completed(argv, 0, b"" if binary else "", b"" if binary else "")
        res = best.result
        if callable(res):
            res = res(argv, input)
        return _completed(argv, res.returncode, res.stdout, res.stderr)

    # Convenience for assertions -------------------------------------------------

    def called(self, prefix: Sequence[str]) -> list[list[str]]:
        """All recorded calls that start with prefix."""
        prefix = list(prefix)
        return [c for c in self.calls if c[: len(prefix)] == prefix]

    def systemctl_calls(self) -> list[list[str]]:
        return self.called(["systemctl"])


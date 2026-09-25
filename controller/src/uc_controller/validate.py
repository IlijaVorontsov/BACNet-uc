"""Running validators against the staged files (DESIGN.md §5.4 step 4).

``{staged}`` in a validator's argv is replaced by the staging root. A
validator whose ``needs`` executable is missing is skipped with a warning.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import asdict, dataclass

from .roles.base import Validator
from .runner import Runner

log = logging.getLogger(__name__)

TAIL = 2000


@dataclass
class ValidatorResult:
    name: str
    role: str
    argv: list[str]
    rc: int | None
    ok: bool
    skipped: bool = False
    stderr: str = ""          # tail of stderr (stdout if stderr is empty)
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def substitute(argv: list[str], staged: str) -> list[str]:
    return [str(a).replace("{staged}", staged) for a in argv]


def run_one(role: str, v: Validator, staged: str, runner: Runner) -> ValidatorResult:
    argv = substitute(v.argv, staged)
    if v.needs and runner.which(v.needs) is None:
        log.warning("validator %s/%s skipped: %s is not installed", role, v.name, v.needs)
        return ValidatorResult(v.name, role, argv, None, ok=True, skipped=True,
                               stderr=f"skipped: {v.needs} not installed")
    start = time.monotonic()
    try:
        res = runner.run(argv, check=False, timeout=v.timeout_s)
        rc = res.returncode
        text = (res.stderr or "").strip() or (res.stdout or "").strip()
    except subprocess.TimeoutExpired:
        rc, text = None, f"timed out after {v.timeout_s:g} s"
    ok = rc is not None and rc in v.ok_codes
    result = ValidatorResult(v.name, role, argv, rc, ok=ok, stderr=text[-TAIL:],
                             seconds=round(time.monotonic() - start, 3))
    if ok:
        log.info("validator %s/%s ok", role, v.name)
    else:
        log.error("validator %s/%s failed (rc=%s): %s", role, v.name, rc, text[-500:])
    return result


def run_validators(items: list[tuple[str, Validator]], staged: str, runner: Runner) -> list[ValidatorResult]:
    """Run (role name, Validator) pairs in order; all of them, even after a failure."""
    return [run_one(role, v, staged, runner) for role, v in items]

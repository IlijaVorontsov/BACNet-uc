"""Well-known paths of uc-controller (DESIGN.md §5.7 and §19.5).

Every path is an absolute path on the target system. When the environment
variable ``UC_ROOT`` is set (tests), every path is prefixed with it, so a test
can run the whole apply transaction inside a temporary directory.

Resources that ship with the package (templates, the schema, historian
migrations, helper scripts) are found in the source tree when
``UC_CONTROLLER_SRC`` points at ``controller/``, and in the installed
locations otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path

# Unprefixed system paths. Use Paths (below) to get the UC_ROOT-aware form.
ETC = "/etc/uc-controller"
CONTROLLER_YAML = ETC + "/controller.yaml"
DEVICES_YAML = ETC + "/devices.yaml"
SECRETS_DIR = ETC + "/secrets"
HOLD = ETC + "/hold"
RELEASE = ETC + "/release"
KNOWN_HOSTS = ETC + "/known_hosts"
TLS_DIR = ETC + "/tls"
PKI_DIR = "/etc/uc-pki"
SSH_DIR = "/etc/ssh"
HUB_SITE_YAML = "/etc/uc-hub/site.yaml"
HUB_DATA = "/var/lib/uc-hub"
MOSQUITTO_DATA = "/var/lib/mosquitto"
JOURNAL = "/var/log/journal"

STATE = "/var/lib/uc-controller"
STAGING = STATE + "/staging"
LKG = STATE + "/lkg"
INSTALLED_JSON = STATE + "/installed.json"
LAST_APPLY = STATE + "/last-apply.json"
PENDING_CONFIRM = STATE + "/pending-confirm.json"
APPLY_LOCK = STATE + "/apply.lock"
FIRSTBOOT = STATE + "/firstboot"
BOOT_HISTORY = STATE + "/boot-history.jsonl"
BOARD_JSON = STATE + "/board.json"
HEALTH_STATE = STATE + "/health-state.json"
BACKUP_OK = STATE + "/backup.ok"
RELEASES = STATE + "/releases"
SNAPSHOTS = STATE + "/snapshots"
METRICS = STATE + "/metrics"
BUNDLES_FALLBACK = STATE + "/bundles"
PENDING_EVENTS = STATE + "/pending-events.jsonl"
CLEAN_SHUTDOWN = STATE + "/clean-shutdown"
ALIVE = STATE + "/.alive"

RUN = "/run/uc-controller"
CHRONY_LOCAL = RUN + "/chrony-local.conf"
LIVENESS_SUPPRESSED = RUN + "/liveness-suppressed"
TIME_UNTRUSTED = RUN + "/time-untrusted"
RUN_HEALTH = "/run/uc-health"
RUN_HISTORIAN = "/run/uc-historian"

SRV = "/srv/uc"
SRV_BACKUP = SRV + "/backup"
SRV_BUNDLES = SRV_BACKUP + "/bundles"
SRV_REPORTS = SRV + "/reports"
SRV_ALIVE = SRV + "/.alive"
PG_DATA = SRV + "/postgresql/18/main"

BOOT_FIRMWARE = "/boot/firmware"
FIRSTBOOT_YAML = BOOT_FIRMWARE + "/uc-controller.yaml"
DEV_RTC = "/dev/rtc0"
FAKE_HWCLOCK_DATA = "/etc/fake-hwclock.data"

SHARE = "/usr/share/uc-controller"
LIBEXEC = "/usr/lib/uc-controller"
UC_CTL = "/usr/sbin/uc-ctl"


def uc_root() -> str:
    """The test prefix from UC_ROOT, without a trailing slash ('' when unset)."""
    return os.environ.get("UC_ROOT", "").rstrip("/")


def p(path: str) -> str:
    """Return ``path`` with the UC_ROOT prefix applied."""
    if not path.startswith("/"):
        raise ValueError(f"not an absolute path: {path!r}")
    return uc_root() + path


class Paths:
    """UC_ROOT-aware access to the well-known paths.

    ``Paths().controller_yaml`` is ``$UC_ROOT/etc/uc-controller/controller.yaml``.
    The prefix is read once, when the object is created.
    """

    _NAMES = {
        "etc": ETC, "controller_yaml": CONTROLLER_YAML, "devices_yaml": DEVICES_YAML,
        "secrets_dir": SECRETS_DIR, "hold": HOLD, "release": RELEASE,
        "known_hosts": KNOWN_HOSTS, "tls_dir": TLS_DIR, "pki_dir": PKI_DIR,
        "ssh_dir": SSH_DIR, "hub_site_yaml": HUB_SITE_YAML, "hub_data": HUB_DATA,
        "mosquitto_data": MOSQUITTO_DATA, "journal": JOURNAL,
        "state": STATE, "staging": STAGING, "lkg": LKG, "installed_json": INSTALLED_JSON,
        "last_apply": LAST_APPLY, "pending_confirm": PENDING_CONFIRM,
        "apply_lock": APPLY_LOCK, "firstboot": FIRSTBOOT, "boot_history": BOOT_HISTORY,
        "board_json": BOARD_JSON, "health_state": HEALTH_STATE, "backup_ok": BACKUP_OK,
        "releases": RELEASES, "snapshots": SNAPSHOTS, "metrics": METRICS,
        "bundles_fallback": BUNDLES_FALLBACK, "pending_events": PENDING_EVENTS,
        "clean_shutdown": CLEAN_SHUTDOWN, "alive": ALIVE,
        "run": RUN, "chrony_local": CHRONY_LOCAL,
        "liveness_suppressed": LIVENESS_SUPPRESSED, "time_untrusted": TIME_UNTRUSTED,
        "run_health": RUN_HEALTH, "run_historian": RUN_HISTORIAN,
        "srv": SRV, "srv_backup": SRV_BACKUP, "srv_bundles": SRV_BUNDLES,
        "srv_reports": SRV_REPORTS, "srv_alive": SRV_ALIVE, "pg_data": PG_DATA,
        "boot_firmware": BOOT_FIRMWARE, "firstboot_yaml": FIRSTBOOT_YAML,
        "dev_rtc": DEV_RTC, "fake_hwclock_data": FAKE_HWCLOCK_DATA,
    }

    def __init__(self, root: str | None = None):
        self.root = uc_root() if root is None else root.rstrip("/")
        for name, path in self._NAMES.items():
            setattr(self, name, self.root + path)

    def p(self, path: str) -> str:
        """Prefix an arbitrary absolute path with this object's root."""
        if not path.startswith("/"):
            raise ValueError(f"not an absolute path: {path!r}")
        return self.root + path

    def unprefix(self, path: str) -> str:
        """Inverse of p(): the system path of a prefixed path."""
        if self.root and path.startswith(self.root + "/"):
            return path[len(self.root):]
        return path

    def __repr__(self) -> str:
        return f"Paths(root={self.root!r})"


def current() -> Paths:
    return Paths()


# ---------------------------------------------------------------------------
# Resources shipped with the package


def src_dir() -> Path | None:
    """The ``controller/`` source directory, or None when running installed.

    UC_CONTROLLER_SRC wins. Without it, a source checkout is recognised by the
    schema file two levels above this package (controller/src/uc_controller).
    """
    env = os.environ.get("UC_CONTROLLER_SRC")
    if env:
        return Path(env)
    guess = Path(__file__).resolve().parents[2]
    if (guess / "site" / "controller.schema.json").is_file() and (guess / "templates").is_dir():
        return guess
    return None


def templates_dir() -> Path:
    src = src_dir()
    return src / "templates" if src else Path(SHARE) / "templates"


def schema_path() -> Path:
    src = src_dir()
    return src / "site" / "controller.schema.json" if src else Path(SHARE) / "controller.schema.json"


def site_dir() -> Path:
    """Directory with the schema and (in the source tree) site/examples/."""
    src = src_dir()
    return src / "site" if src else Path(SHARE)


def examples_dir() -> Path:
    return site_dir() / "examples"


def migrations_dir() -> Path:
    src = src_dir()
    return src / "historian" / "migrations" if src else Path(SHARE) / "historian" / "migrations"


def helper(name: str) -> Path:
    """A helper program from /usr/lib/uc-controller (rootfs/ in the source tree)."""
    src = src_dir()
    if src and (src / "rootfs" / LIBEXEC.lstrip("/") / name).exists():
        return src / "rootfs" / LIBEXEC.lstrip("/") / name
    return Path(LIBEXEC) / name


# ---------------------------------------------------------------------------
# File-system helpers shared by apply, secrets and other writers


def fsync_dir(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: str, data: bytes | str, mode: int = 0o644,
                 uid: int | None = None, gid: int | None = None,
                 dir_mode: int = 0o755) -> None:
    """Write a file so that readers see either the old or the new content.

    Temp file in the same directory, fsync, chmod/chown, rename, fsync of the
    directory. Missing parent directories are created with dir_mode.
    """
    if isinstance(data, str):
        data = data.encode()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=dir_mode, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.uc-tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if uid is not None or gid is not None:
            os.chown(tmp, -1 if uid is None else uid, -1 if gid is None else gid)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(directory)

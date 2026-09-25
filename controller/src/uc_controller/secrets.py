"""Secret store (DESIGN.md §13, §19.4).

Secrets are files in ``/etc/uc-controller/secrets`` (directory 0700, files
0600, owner root). Names match ``^[a-z0-9-]+$``. Generated kinds:

- ``password``: 32 characters from [A-Za-z0-9_-]
- ``token``: ``secrets.token_urlsafe(32)`` (43 characters)
- ``hex32``: 32 random bytes as 64 hex characters
- ``ssh-ed25519``: ``ssh-keygen -t ed25519 -N ''``; the private key is the
  secret, the public key is written next to it as ``<name>.pub``

``ensure`` never replaces an existing secret.
"""

from __future__ import annotations

import logging
import os
import re
import secrets as _random
import string
import tempfile

from . import paths as _paths
from .runner import Runner

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9-]+$")
KINDS = ("password", "token", "hex32", "ssh-ed25519")
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "_-"

# The fixed set that apply ensures (§5.4 step 2); hub tokens are added per user.
STANDARD = (
    ("mqtt-uc-hub", "password"),
    ("mqtt-uc-historian", "password"),
    ("mqtt-uc-health", "password"),
    ("backup-ssh-key", "ssh-ed25519"),
)


class SecretError(Exception):
    pass


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise SecretError(f"invalid secret name {name!r} (allowed: a-z, 0-9, '-')")
    return name


def generate(kind: str) -> str:
    """A new random value of a text kind (not ssh-ed25519)."""
    if kind == "password":
        return "".join(_random.choice(PASSWORD_ALPHABET) for _ in range(32))
    if kind == "token":
        return _random.token_urlsafe(32)
    if kind == "hex32":
        return _random.token_hex(32)
    raise SecretError(f"unknown secret kind {kind!r}")


def standard_secrets(site) -> list[tuple[str, str]]:
    """(name, kind) pairs apply ensures for a site: §13 plus one token per hub user."""
    wanted = list(STANDARD)
    users = site.services.hub.users if site is not None else ()
    for u in users:
        wanted.append((f"hub-token-{u.user}", "token"))
    return wanted


class SecretStore:
    """File-backed secrets. ``root`` defaults to the UC_ROOT-aware secrets dir."""

    def __init__(self, directory: str | None = None, runner: Runner | None = None):
        self.dir = directory or _paths.current().secrets_dir
        self.runner = runner or Runner()

    # -- helpers --------------------------------------------------------------

    def _ensure_dir(self) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, check_name(name))

    def system_path(self, name: str) -> str:
        """The path on the target system (without UC_ROOT), for rendered files."""
        return _paths.SECRETS_DIR + "/" + check_name(name)

    # -- interface (§19.4) ------------------------------------------------------

    def exists(self, name: str) -> bool:
        try:
            return os.path.getsize(self.path(name)) > 0
        except FileNotFoundError:
            return False

    def get(self, name: str) -> str:
        """The secret as text; one trailing newline is removed."""
        try:
            with open(self.path(name), encoding="utf-8") as fh:
                value = fh.read()
        except FileNotFoundError:
            raise SecretError(f"secret {name!r} does not exist") from None
        return value[:-1] if value.endswith("\n") else value

    def set(self, name: str, value: str | bytes) -> None:
        self._ensure_dir()
        _paths.atomic_write(self.path(name), value, mode=0o600, dir_mode=0o700)

    def names(self) -> list[str]:
        try:
            entries = os.listdir(self.dir)
        except FileNotFoundError:
            return []
        return sorted(n for n in entries
                      if NAME_RE.match(n) and os.path.isfile(os.path.join(self.dir, n)))

    def ensure(self, name: str, kind: str) -> bool:
        """Create the secret if it does not exist. Returns True if created."""
        check_name(name)
        if kind not in KINDS:
            raise SecretError(f"unknown secret kind {kind!r}")
        if self.exists(name):
            return False
        if kind == "ssh-ed25519":
            self._new_ssh_key(name)
        else:
            self.set(name, generate(kind))
        log.info("secret %s created (%s)", name, kind)
        return True

    def _new_ssh_key(self, name: str) -> None:
        self._ensure_dir()
        # ssh-keygen writes into a temp dir inside the secrets dir, then both
        # files are renamed into place (same file system, atomic).
        tmpdir = tempfile.mkdtemp(prefix=".keygen-", dir=self.dir)
        try:
            key = os.path.join(tmpdir, "key")
            self.runner.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"uc-controller-{name}",
                             "-f", key], input="")
            os.chmod(key, 0o600)
            os.chmod(key + ".pub", 0o644)
            os.replace(key + ".pub", self.path(name) + ".pub")
            os.replace(key, self.path(name))
            _paths.fsync_dir(self.dir)
        finally:
            for f in os.listdir(tmpdir):
                os.unlink(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)

    def ensure_standard(self, site) -> list[str]:
        """Ensure every secret apply needs; returns the names that were created."""
        return [n for n, k in standard_secrets(site) if self.ensure(n, k)]


class ReadOnlySecrets:
    """Wraps a store for --dry-run: never writes, placeholders for missing secrets."""

    PLACEHOLDER = "<not generated yet>"

    def __init__(self, store: SecretStore):
        self._store = store

    def path(self, name: str) -> str:
        return self._store.path(name)

    def system_path(self, name: str) -> str:
        return self._store.system_path(name)

    def exists(self, name: str) -> bool:
        return self._store.exists(name)

    def get(self, name: str) -> str:
        return self._store.get(name) if self._store.exists(name) else self.PLACEHOLDER

    def names(self) -> list[str]:
        return self._store.names()

    def ensure(self, name: str, kind: str) -> bool:
        return False

    def set(self, name: str, value) -> None:
        raise SecretError("dry run: secrets are not written")

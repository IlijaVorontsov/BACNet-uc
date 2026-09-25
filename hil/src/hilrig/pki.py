"""Per-run TLS certificates from the committed TEST-ONLY CA in hil/pki/.

hil/pki/mkpki.sh does the work (one implementation for shell users and for pytest); this
module runs it and names the resulting files. See hil/pki/README.md for what each case is for.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PKI_DIR = Path(__file__).resolve().parents[2] / "pki"
SERVER_CASES = ("good", "wrongname", "expired", "revoked", "rogue")
CLIENT_CASES = ("rogue", "revoked")


@dataclass(frozen=True)
class CertPair:
    """A certificate and its private key (PEM)."""

    cert: Path
    key: Path


@dataclass(frozen=True)
class Pki:
    """The test CA, the committed DUT client certificate and one run's certificates."""

    root: Path
    ca: Path
    crl: Path
    rogue_ca: Path
    dut: CertPair
    servers: Mapping[str, CertPair]
    clients: Mapping[str, CertPair]

    @classmethod
    def generate(
        cls,
        out: Path,
        *,
        broker_name: str = "broker.hil.lan",
        broker_ip: str = "192.0.2.1",
        source: Path = PKI_DIR,
    ) -> Pki:
        """Issue the per-run certificates into ``out`` and return their paths."""
        out.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "HIL_BROKER_NAME": broker_name, "HIL_BROKER_IP": broker_ip}
        proc = subprocess.run(
            [str(source / "mkpki.sh"), str(out)],
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"mkpki.sh failed ({proc.returncode}): {proc.stderr.strip()}")

        def pair(name: str) -> CertPair:
            return CertPair(out / f"{name}.crt", out / f"{name}.key")

        return cls(
            root=out,
            ca=out / "ca.crt",
            crl=out / "ca.crl",
            rogue_ca=out / "rogue-ca.crt",
            dut=CertPair(source / "dut-client.crt", source / "dut-client.key"),
            servers={case: pair(f"srv-{case}") for case in SERVER_CASES},
            clients={case: pair(f"client-{case}") for case in CLIENT_CASES},
        )

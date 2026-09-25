"""TEST-ONLY PKI (hil/pki, hilrig.pki): every certificate verifies, or fails, as intended."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hilrig.pki import CLIENT_CASES, PKI_DIR, SERVER_CASES, Pki
from rig_skips import needs_tools

pytestmark = needs_tools("openssl")


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    return Pki.generate(tmp_path_factory.mktemp("pki"))


def openssl(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["openssl", *map(str, args)], capture_output=True, text=True, timeout=30, check=False
    )


def verify(pki: Pki, cert: Path, purpose: str, *, hostname: str | None = None, crl: bool = True) -> str:
    """Return openssl verify's verdict: ``OK`` or the error text."""
    args: list[str | Path] = ["verify", "-CAfile", pki.ca, "-purpose", purpose]
    if crl:
        args += ["-CRLfile", pki.crl, "-crl_check"]
    if hostname:
        args += ["-verify_hostname", hostname]
    proc = openssl(*args, cert)
    return "OK" if proc.returncode == 0 else proc.stdout + proc.stderr


def test_committed_ca_is_a_ten_year_p256_root() -> None:
    text = openssl("x509", "-in", PKI_DIR / "ca.crt", "-noout", "-text").stdout
    assert "Subject: CN = HIL TEST-ONLY Root CA" in text and "NIST CURVE: P-256" in text
    assert "CA:TRUE" in text and "Certificate Sign, CRL Sign" in text
    dates = openssl("x509", "-in", PKI_DIR / "ca.crt", "-noout", "-startdate", "-enddate").stdout
    start, end = (int(line.split()[-2]) for line in dates.splitlines())
    assert end - start == 10
    assert (
        openssl("x509", "-in", PKI_DIR / "ca.crt", "-noout", "-checkend", str(5 * 365 * 86400)).returncode
        == 0
    )


def test_committed_keys_match_their_certificates() -> None:
    for name in ("ca", "dut-client"):
        cert_key = openssl("x509", "-in", PKI_DIR / f"{name}.crt", "-noout", "-pubkey").stdout
        key = openssl("pkey", "-in", PKI_DIR / f"{name}.key", "-pubout").stdout
        assert cert_key == key and key


def test_readme_warns_loudly() -> None:
    readme = (PKI_DIR / "README.md").read_text()
    assert "TEST-ONLY" in readme and "every private key in this directory is public" in readme


def test_dut_client_certificate(pki: Pki) -> None:
    assert verify(pki, pki.dut.cert, "sslclient") == "OK"
    assert "CN = hil-dut" in openssl("x509", "-in", pki.dut.cert, "-noout", "-subject").stdout


def test_all_cases_generated(pki: Pki) -> None:
    for pair in (*pki.servers.values(), *pki.clients.values()):
        assert pair.cert.stat().st_size and pair.key.stat().st_size
    assert set(pki.servers) == set(SERVER_CASES) and set(pki.clients) == set(CLIENT_CASES)


@pytest.mark.parametrize(
    ("case", "verdict"),
    [
        ("good", "OK"),
        ("wrongname", "hostname mismatch"),
        ("expired", "certificate has expired"),
        ("revoked", "certificate revoked"),
        ("rogue", "unable to get local issuer certificate"),
    ],
)
def test_server_certificates(pki: Pki, case: str, verdict: str) -> None:
    result = verify(pki, pki.servers[case].cert, "sslserver", hostname="broker.hil.lan")
    assert verdict in result


def test_server_certificate_names(pki: Pki) -> None:
    good = openssl("x509", "-in", pki.servers["good"].cert, "-noout", "-ext", "subjectAltName").stdout
    assert "DNS:broker.hil.lan" in good and "IP Address:192.0.2.1" in good
    assert verify(pki, pki.servers["good"].cert, "sslclient") != "OK"  # serverAuth only
    assert verify(pki, pki.servers["revoked"].cert, "sslserver", crl=False) == "OK"  # only the CRL rejects it


@pytest.mark.parametrize(
    ("case", "verdict"),
    [
        ("revoked", "certificate revoked"),
        ("rogue", "unable to get local issuer certificate"),
    ],
)
def test_client_certificates(pki: Pki, case: str, verdict: str) -> None:
    assert verdict in verify(pki, pki.clients[case].cert, "sslclient")


def test_crl_is_signed_by_the_test_ca(pki: Pki) -> None:
    proc = openssl("crl", "-in", pki.crl, "-CAfile", pki.ca, "-noout")
    assert proc.returncode == 0 and "verify OK" in proc.stdout + proc.stderr
    serials = openssl("crl", "-in", pki.crl, "-noout", "-text").stdout.count("Serial Number:")
    assert serials == 2


def test_runs_do_not_share_serials(pki: Pki, tmp_path: Path) -> None:
    other = Pki.generate(tmp_path / "pki2")

    def serial(p: Pki) -> str:
        return openssl("x509", "-in", p.servers["good"].cert, "-noout", "-serial").stdout

    assert serial(pki) != serial(other)
    assert pki.rogue_ca.read_bytes() != other.rogue_ca.read_bytes()


def test_generation_failure_is_reported(tmp_path: Path) -> None:
    empty = tmp_path / "no-ca"
    empty.mkdir()
    (empty / "mkpki.sh").write_bytes((PKI_DIR / "mkpki.sh").read_bytes())
    (empty / "mkpki.sh").chmod(0o755)
    with pytest.raises(RuntimeError, match="committed test CA missing"):
        Pki.generate(tmp_path / "out", source=empty)

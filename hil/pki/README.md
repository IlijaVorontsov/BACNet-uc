# TEST-ONLY PKI of the HIL rig

> **WARNING: every private key in this directory is public.** `ca.key` and
> `dut-client.key` are committed on purpose so that anyone can run the rig. Anything signed
> by this CA must be treated as untrusted. Never add `ca.crt` to a trust store outside the
> rig, never flash a production device with `dut-client.*`, and never reuse these files
> for a real broker.

| File | What it is |
|---|---|
| `ca.crt`, `ca.key` | Test root CA "HIL TEST-ONLY Root CA", EC P-256, valid 10 years (2026-2036) |
| `dut-client.crt`, `dut-client.key` | DUT client certificate (CN `hil-dut`, clientAuth), signed by the test CA |
| `mkpki.sh` | Issues the per-run certificates; `--new-ca` regenerates the two pairs above |

The DUT's HIL build trusts `ca.crt` and, for mutual TLS, presents `dut-client.*`
(for `apps/mqtt_tls`: `CONFIG_APP_MQTT_TLS_CA_CERT_FILE`, `CONFIG_APP_MQTT_TLS_CLIENT_AUTH=y`,
`CONFIG_APP_MQTT_TLS_CLIENT_CERT_FILE`, `CONFIG_APP_MQTT_TLS_CLIENT_KEY_FILE`, and
`CONFIG_APP_MQTT_TLS_HOSTNAME="broker.hil.lan"` when the broker is addressed by IP).

## Per-run certificates

`hilrig.pki.Pki.generate(dir)` (the `pki` fixture) runs `mkpki.sh <dir>` once per test
session and writes into the run's artifacts:

| Certificate | Purpose | Expected verdict (checked by `tests/unit/test_pki.py`) |
|---|---|---|
| `srv-good` | Broker, SAN `broker.hil.lan` + IP `192.0.2.1` | valid |
| `srv-wrongname` | Broker, SAN `other.example` | host-name mismatch (error 62) |
| `srv-expired` | Broker, 2020-01-01 to 2021-01-01 (`openssl ca -startdate/-enddate`) | expired (error 10) |
| `srv-revoked` | Broker, revoked in `ca.crl` | revoked (error 23) with CRL checking |
| `srv-rogue` | Broker, right names, signed by `rogue-ca` | unknown issuer (error 20) |
| `client-rogue` | Client, signed by `rogue-ca` | unknown issuer (error 20) |
| `client-revoked` | Client, test CA, revoked in `ca.crl` | revoked (error 23) with CRL checking |
| `ca.crl` | CRL of the test CA, valid 30 days | signature verifies with `ca.crt` |

Serial numbers are random, so runs never collide. After `mkpki.sh --new-ca`, rebuild every
DUT image, because the firmware embeds `ca.crt` and `dut-client.*`.

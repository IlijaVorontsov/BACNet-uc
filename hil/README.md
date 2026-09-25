# hilrig: host side of the BACnet-uc HIL rig

`hilrig` is the Python package (src layout, Python >= 3.11) and pytest suite that drive the
hardware-in-the-loop rig. The rig tests the BACnet firmware (`firmware/`) and the MQTT client
(`apps/mqtt_tls`) on real boards. The fixed decisions of the rig design (v2 baseline B1-B13)
that this package relies on are:

- **DUTs.** P1 `nucleo_f767zi` is the canonical DUT. P2 is `frdm_mcxn947/mcxn947/cpu0`. P3 is
  `nucleo_h563zi`, for MQTT only. All run Zephyr 4.4.2.
- **Stimulus.** A second NUCLEO-F767ZI runs `apps/hil_stimulus` and speaks stimulus protocol v0.
- **Network.** The DUT's NIC sits in netns `lan-a` (bridge `br-a`). Services run in `svc`
  (192.0.2.1), simulated devices in `sim1`/`sim2`, and the router in `rtr`. Subnet B
  (198.51.100.0/24) holds `fd` and `bbmdb`. dnsmasq gives the DUT 192.0.2.10.
- **Test language.** pytest. Twister builds and flashes in CI.

## Quickstart

```sh
python3.12 -m venv ~/.venvs/hil && . ~/.venvs/hil/bin/activate
pip install -e 'hil[dev]'                   # dev: ruff, mypy; add ,saleae for the Logic 2 backend
sudo hil/host/install-net-wrappers.sh       # once per host, see "Privileges"

# bacnet-stack tools at the firmware's SHA (default location hil/tools/bacnet-stack/bin)
git clone https://github.com/bacnet-stack/bacnet-stack.git hil/tools/bacnet-stack
git -C hil/tools/bacnet-stack checkout 54544d02
make -C hil/tools/bacnet-stack bip          # BACDL=bip, BBMD=full; do not use make -j

cd hil
pytest                                       # module tests; namespace tests need root
sudo -E "$(command -v pytest)" --sil         # SIL tier: rig self-validation, no hardware
```

A root `--sil` run on a host with every tool installed runs the unit, rig and SIL tests.
Tests skip, with the reason, when a tool is missing (bacnet-stack, sigrok-cli, chronyd, the
docker image, logic2-automation, gcc and bacnet-stack sources), and every `hil_only`
MS/TP test skips without the real rig.

Host packages (Ubuntu 24.04): `iproute2 tshark dnsmasq mosquitto mosquitto-clients openssl
sigrok-cli`, plus `ethtool` for a real DUT NIC and `chrony` if NTP is wanted. The broker's TLS
key log needs mosquitto >= 2.1, and Ubuntu ships 2.0.18. Pull the image once
(`docker pull eclipse-mosquitto:2.1.2-alpine`) and the broker runs from it inside netns
`svc`. Without the image the broker runs without a key log, and the tests that decrypt skip
with that reason.

## Running against a bench

A bench file describes one DUT and what is wired to it. Copy `host/bench.yml.example` (P1)
to `/etc/hil/<bench>/bench.yml`, then fill in the serial ports, the probe ID and the DUT's
**real MAC**. The MAC keys the dnsmasq reservation for 192.0.2.10: learn it once from the
DUT's console. `Bench.load()` rejects unknown keys and bad values, and names the key in the
error (`bench.yml: dut.mac: '02:80:e1' is not a MAC address ...`).

```sh
sudo -E "$(command -v pytest)" --hil --bench /etc/hil/bench1/bench.yml   # or HIL_BENCH=...
```

| Option | Meaning |
|---|---|
| `--bench FILE` / `$HIL_BENCH` | bench.yml to use (its `mode` must match `--hil`/`--sil`) |
| `--hil` | real rig; without a bench, hardware tests skip and say why |
| `--sil` | SIL tier with `host/bench-sil.yml` (bacserv stand-in DUT) unless `--bench` |
| `--artifacts DIR` / `$HIL_ARTIFACTS` | output directory (default `/tmp/hil-artifacts/<UTC time>`) |
| `--enable-slow`, `--enable-destructive` | run tests with those marks |
| `$HIL_BACNET_BIN` | bacnet-stack tool directory |
| `$HIL_BACNET_STACK` | bacnet-stack sources: the MS/TP frames are checked byte for byte against `MSTP_Create_Frame()` (skipped without them, or found via `$ZEPHYR_BASE/../modules/lib/bacnet/stack`) |

The artifacts directory collects `pcap/<test>.pcapng`, with TLS secrets injected when the
broker's key log exists. It also holds the service logs and configs (`services/`), the
per-run certificates (`pki/`), `stim.log` (stimulus traffic), the logic analyzer captures
(`la/`) and the rig reports (`rig/R-01.json`, written by every run, also without a bench).
A run leaves nothing else behind outside it and pytest's temporary directories: no
namespaces, processes or containers, and the services keep their lease, pid and socket
files (or have none) in `services/`.

## Layout

```
hil/
  pyproject.toml          package, dependencies, pytest, ruff and mypy configuration
  src/hilrig/
    bench.py              Bench.load(bench.yml): validated dataclasses
    stim.py               Stim: stimulus protocol v0 driver (typed replies, StimError(errno))
    capture.py            Capture: per-test pcapng in a netns, sentinel barrier, rows()
    netns.py              Topology up/down (wrappers), run/spawn/enter inside a netns
    services.py           Dnsmasq, Mosquitto (key log, docker 2.1 fallback), Chrony, TlsServer
    bacnet.py             Bacnet (bacwi, bacrp, bacwp, bacrpm, bacrfdt, bacrbdt, bacepics), BacServ
    mqtt.py               MqttClient, Device: bacnet-uc/<id>/{status,info,telemetry,cmd,event}
    pki.py                Pki.generate(): per-run certificates from the TEST-ONLY CA
    mstp.py la.py sync.py MS/TP frames and timing, logic analyzers, clock fit
  net/up.sh down.sh       topology scripts, installed as hil-net-up / hil-net-down
  pki/                    TEST-ONLY CA and DUT client certificate, mkpki.sh (see pki/README.md)
  host/                   bench.yml.example (P1), bench-sil.yml, install-net-wrappers.sh
  decoders/ saleae/       sigrok and Logic 2 MS/TP decoders
  tests/
    conftest.py           fixtures, options and marks
    unit/                 module tests (no hardware); fake_stim.py, fake_mqtt_dut.py
    rig/                  R-01 stimulus link, R-04 capture pipeline
    sil/                  rig self-validation with a bacserv stand-in DUT
    mstp/                 MS/TP bench-bus tests (hil_only); mstp_bus.py: the host's MS/TP node
```

## Topology

```
 subnet A 192.0.2.0/24 (br-a in lan-a)                  subnet B 198.51.100.0/24 (br-b in lan-b)
   DUT .10   real NIC (--dut-iface), TAP zeth (--sil) or netns dut (--standin)
   svc .1    dnsmasq, mosquitto 8883 (TLS) + 127.0.0.1:1883, chrony, client tools
   sim1 .11, sim2 .12   simulated devices
   rtr .254 ---------------------------------------------- rtr .254
                                                            fd .10     foreign-device client
                                                            bbmdb .2   peer BBMD
```

`net/up.sh` is idempotent. It moves a real NIC into `lan-a`, turns its offloads off (so the
capture shows the wire), and refuses to move the NIC that carries the default route.
`net/down.sh` stops leftover processes, returns the NIC and deletes the namespaces.
`--prefix P` builds a private copy: the module tests use `hu-` and `hn-`.
`Topology.session()` brings a topology up for a block and takes it down afterwards, also
when `up.sh` fails half-way, unless it was already up (someone else's rig).

## Privileges

`host/install-net-wrappers.sh` installs root-owned copies of `net/up.sh` and `net/down.sh`
as `/usr/local/sbin/hil-net-up` and `hil-net-down`, with a sudoers rule that lets group `hil`
run exactly those two. Their arguments are validated inside them. As root, `hilrig.netns`
runs the scripts in the checkout; as any other user it runs `sudo -n hil-net-up`.

sudoers never allows `ip netns exec`, which amounts to a root shell, and never allows
scripts from a work tree, which anyone who can push could edit. Running processes inside a
namespace needs CAP_SYS_ADMIN, so the test session itself runs as root. In CI that happens
in the privileged job container. On Ubuntu 24.04, dumpcap is `/usr/bin/dumpcap`.

## Fixtures

| Fixture | Scope | Provides |
|---|---|---|
| `bench` | session | the validated bench (skips without one); `selected_bench` may be None |
| `artifacts` | session | the run's output directory |
| `netns` | session | `Topology` for the bench, brought up (and down if this session did, even after a failed bring-up) |
| `pki` | session | `Pki`: test CA, DUT client cert, srv-good/wrongname/expired/revoked/rogue, CRL |
| `services` | session | `RigServices`: dnsmasq (DUT reservation), mosquitto (test PKI, CRL), chrony |
| `stim` | session | `Stim` (skips without a `stim` section); autouse `safe()` before/after each test |
| `la` | session | `hilrig.la` analyzer for `la.driver`/`la.channels`; `la.acquire(seconds, workdir)` per test |
| `bacnet` | session | `Bacnet` in netns svc; `.foreign(bbmd_ip)` for subnet B clients |
| `capture` | function | running `Capture` on the DUT's wire; `@pytest.mark.capture("bpf")` narrows it; a malformed frame from the DUT fails the test |
| `mqtt` | function | `Device` for `dut.mqtt_client_id` on the broker's local listener |
| `unit_net` | session | private topology `hu-` for module tests |

## Marks

| Mark | Effect |
|---|---|
| `hil_only` | skipped unless `--hil` with a hil bench |
| `timing` | timing assertion, hardware only (skipped in SIL) |
| `slow` | needs `--enable-slow` |
| `destructive` | needs `--enable-destructive` (power cuts during flash writes; use the spare DUT) |
| `mstp` | needs an `mstp` section (RS-485 bus) in the bench |
| `release` | tier (a): runs against the unmodified release artifact; select with `-m release` |
| `capture(bpf)` | capture filter for the `capture` fixture |

## Stimulus protocol v0

This is the Zephyr shell group `stim`. The prompt is `stim:~$ ` with VT100 off. Each command
gets one reply line, `OK k=v ...` or `ERR <negative errno> <text>`. The boot banner is
`STIM READY proto=0 fw=<semver> board=<board> rc=<reset-cause>`.

`Stim` ignores echo, log lines and escape codes, and returns typed values (`StimInfo`,
`Pulse`, `Edges`, `AdcReading`, ints). It raises `StimError(errno, text)` on `ERR`, and
`StimLinkError` when the link fails: no reply in time, not exactly one OK/ERR line, a reply
with a missing or unconvertible field, a write the board does not take within the timeout,
or a port that vanished. State-changing commands are never retried. Read-only commands
(`info`, `din`, `edges`, `adc`) are retried once. After any link error the next command
first resynchronises with the prompt (`sync()`), so the late reply of a timed-out command
is never taken for the reply of the next one. Channel names are the DUT catalog names
(`di1`, `do3`, `ai0`, `ao0`) plus `nrst`, `nrst_sense`, `pwr`, `sync` and `m0`..`m3`.

## bacnet-stack tools

`hilrig.bacnet` runs the tools inside a rig namespace with an environment of its own: the
`BACNET_*` variables of the calling shell (`BACNET_IP_PORT`, `BACNET_BBMD_ADDRESS`, ...) are
removed and the wrapper sets `BACNET_IFACE`, the APDU timeout and retries, and, for
`Bacnet.foreign()`, the BBMD registration.

## Development

```sh
cd hil
ruff check . && ruff format --check .        # configuration in pyproject.toml [tool.ruff]
mypy                                         # --strict on src/ ([tool.mypy])
MYPYPATH=tests/unit:tests/mstp mypy --strict tests/conftest.py tests/unit tests/rig
MYPYPATH=tests/unit:tests/mstp mypy --strict tests/sil
MYPYPATH=tests/unit:tests/mstp mypy --strict tests/mstp
```

The test directories are type-checked in three runs because mypy cannot hold two modules
named `conftest` at once.

The sigrok decoder (`decoders/`) and the Logic 2 extension (`saleae/`) are checked as
Python 3.8 code in their hosts' style. Every fix made during integration has a regression
test that names the defect in its docstring (`Regression: ...`).

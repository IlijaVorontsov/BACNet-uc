# hilrig: host side of the BACnet-uc HIL rig

`hilrig` is the Python package (src layout, Python >= 3.11) and pytest suite that drive the
hardware-in-the-loop rig. The rig tests the BACnet firmware (`firmware/`) and the MQTT client
(`apps/mqtt_tls`) on real boards. The fixed decisions of the rig design (v2 baseline B1-B13)
that this package relies on are:

- **DUTs.** P1 `nucleo_f767zi` is the canonical DUT. P2 is `frdm_mcxn947/mcxn947/cpu0`. P3 is
  `nucleo_h563zi`, for MQTT only. All run Zephyr 4.4.2.
- **Stimulus.** A second NUCLEO-F767ZI runs `apps/hil_stimulus` and speaks stimulus protocol v0.
- **Network.** The DUT's NIC sits in netns `lan-a` (bridge `br-a`). Services run in `svc`
  (192.0.2.1, and 192.0.2.2 for the TLS test servers, D33), simulated devices in `sim1`/`sim2`, and the router in `rtr`. Subnet B
  (198.51.100.0/24) holds `fd` and `bbmdb`. dnsmasq gives the DUT 192.0.2.10.
- **Test language.** pytest. Twister builds and flashes in CI.

## Quickstart

```sh
python3.12 -m venv ~/.venvs/hil && . ~/.venvs/hil/bin/activate
pip install -e 'hil[dev,smp]'               # dev: ruff, mypy; smp: SMP/schemas; ,saleae: Logic 2
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
| `--cycles N` / `$HIL_CYCLES` | reset and power-cycle loop count (default 10; nightly 20, weekly 50) |
| `--hil-select EXPR` | marker expression that narrows the scenario's `-m` (`and`, `or`, `not`, parentheses) |
| `--dut-build DIR` | local hardware runs: the image under test; flashed at session start unless `--no-flash` |
| `--dut-app bacnet\|mqtt` | the app already on the DUT, when no build dir is given |
| `--sil-dut APP=EXE` / `$HIL_SIL_DUT` | SIL: a native_sim TAP build as the DUT (see "SIL DUT"); repeat for both apps |
| `--release-build DIR`, `--plain-build DIR` | SEC-01: release builds and the plain builds they are compared with |
| `--mqtt-checkout DIR` / `$HIL_MQTT_CHECKOUT` | S-03: the MQTT branch checkout inside a west workspace |
| `$HIL_BACNET_BIN` | bacnet-stack tool directory |
| `$HIL_BACNET_HARNESS` | `harness/` of a BACnet branch checkout: its SMP client (`smp` fixture, needs `cbor2`) |
| `$HIL_BACNET_SCHEMAS` | that branch's `schemas/` (default: next to the harness); the golden documents are validated against it (needs `jsonschema`) |
| `$HIL_OPENOCD` | OpenOCD for the flash guard R-07 (default: the SDK host tools, then PATH) |
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
    capture.py            Capture: per-test pcapng in a netns, sentinel barrier, rows(), texts()
    netns.py              Topology up/down (wrappers), run/spawn/enter inside a netns, ping
    services.py           Dnsmasq (point_broker, DHCP events), Mosquitto, Chrony, TlsServer, TlsFront
    console.py            Console: DUT console log with marks (serial, Twister's dut, a process)
    smp.py                Smp: the BACnet harness's SMP client, synchronous, run inside netns svc
    rigconfig.py          golden device/io/apps.json (D25) and RigConfig.apply() over SMP
    dutctl.py             DutLink (DHCP, I-Am, MQTT online, HIL-READY), DutControl reset/power
    flash.py              flash guard R-07 (OpenOCD), west flash, USB serial/devnum lookup
    sildut.py             SilDut: a native_sim TAP build as the DUT in netns lan-a
    release.py            DutImage (a build's .config) and the SEC-01 check (D24 allowlist)
    markexpr.py           --hil-select marker expressions
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
    rig/                  R-01, R-02a, R-02b, R-04, R-06, R-07 rig self-tests (R-01/02/04/07 gate)
    system/ reset/ power/ SYS-01, RST-01, RST-04, PWR-01, PWR-02
    mqtt/ tls/ net/       MQTT-01..03, S-03, TLS-01, TLS-03, NET-03
    bacnet_ip/ io/        BIP-01, BIP-03, IO-01, IO-04
    persistence/ release/ PERS-01, SEC-01 (static)
    sil/                  rig self-validation with a bacserv stand-in DUT
    mstp/                 MS/TP bench-bus tests (hil_only); mstp_bus.py: the host's MS/TP node
```

## Topology

```
 subnet A 192.0.2.0/24 (br-a in lan-a)                  subnet B 198.51.100.0/24 (br-b in lan-b)
   DUT .10   real NIC (--dut-iface), TAP zeth (--sil) or netns dut (--standin)
   svc .1    dnsmasq, mosquitto 8883 (TLS) + 127.0.0.1:1883, chrony, client tools
   svc .2    TLS test servers: s_server, the TLS 1.2 front (D33)
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
| `cycles` | session | `--cycles` |
| `app_image` | function | indirect parameter `bacnet`/`mqtt`: the image under test (skips when the DUT runs the other app); in SIL it starts that native_sim DUT, and for BACnet waits for its I-Am and the known state |
| `hil_session` | session, autouse | `--hil`/Twister: stimulus check, R-07 guard, flash inside a capture, wait for the app, `rig_config` (7.3) |
| `dut_console`, `console` | session | the DUT console (`Console`); input only on instrumented images |
| `smp` | session | `Smp` to the BACnet DUT (UDP 1337); skips without the harness |
| `rig_config` | session | the golden documents pushed, verified (SHA-256, Object_Name), reloaded (D25) |
| `dut_link` | function | `DutLink`: DHCP lease, I-Am or a live `online`, HIL-READY |
| `dut_reset`, `dut_power` | function | `DutControl`: `stim reset` / `stim power` (SIL: process restart) |
| `tls_server` | function | factory: `openssl s_server` with a `pki` pair on 192.0.2.2:8883, broker name pointed there |
| `tls_front` | function | factory: TLS 1.2 front on 192.0.2.2:8883 relaying to the broker's 127.0.0.1:1883 |
| `sil_duts` | session | the `--sil-dut` processes; one runs at a time |
| `rig_report` | session | writes `rig/<id>.json` |
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
| `instrumented` | tier (b): needs an instrumented image (`-S hil`); skipped on hardware otherwise |
| `variant` | needs a variant image (a release image plus one documented symbol, D24) |
| `rig(gate=False)` | rig self-test; `gate=True` runs first, and a failure on hardware is a rig fault: the remaining tests skip with the reason, `rig-fault.json` is written |
| `sil` | SIL tier (added to `tests/sil` automatically) |
| `cycles(base_s, per_cycle_s)` | timeout `base_s + per_cycle_s x --cycles` |
| `capture(bpf, iface=...)` | capture filter (and interface, e.g. `br-a`) for the `capture` fixture |

A hardware or Twister session that executes no test fails (a scenario that selects nothing
is a configuration error, not a pass). `StimNoDevice` from the stimulus (a channel the
board has not got) skips the test with the board's text.

## Stimulus protocol v0

This is the Zephyr shell group `stim`. The prompt is `stim:~$ ` with VT100 off. Each command
gets one reply line, `OK k=v ...` or `ERR <negative errno> <text>`. The boot banner is
`STIM READY proto=0 fw=<semver> board=<board> rc=<reset-cause>`.

`Stim` ignores echo, log lines and escape codes, and returns typed values (`StimInfo`,
`ChanInfo`, `Pulse`, `Edges`, `Latency`, `DacReading`, `AdcReading`, `ResetPulse`,
`PowerState`, `RstMon`). It raises `StimError(errno, text)` on `ERR`, as the subclass
`StimTimeout` (-116), `StimNoDevice` (-19), `StimNotSupported` (-134) or `StimBusy` (-16)
where one fits, and `StimLinkError` when the link fails: no reply in time, not exactly one
OK/ERR line, a reply with a missing or unconvertible field, a write the board does not take
within the timeout, or a port that vanished (or is held by another process: the port is
opened exclusively). A boot banner in the middle of a session raises `StimRebooted`: the
board reset, and every output is back at its default. State-changing commands are never
retried. Read-only commands (`info`, `chan`, `din`, `edges`, `adc`, `rstmon`) are retried
once. After any link error the next command first resynchronises (`sync()`: Ctrl-C, which
clears a half-typed line, then a prompt), so the late reply of a timed-out command is never
taken for the reply of the next one. A command line is at most 511 printable ASCII
characters (the shell's buffer); longer or non-printable lines are refused before sending.
Channel names are the DUT catalog names (`di1`, `do3`, `ai0`, `ao0`) plus `nrst`,
`nrst_sense`, `pwr`, `sync`, `lb`, `v3v3`, `v5` and `m0`..`m3`; `hilrig.bench.logical()`
applies a catalog channel's polarity.

## SIL DUT (native_sim)

`--sil-dut bacnet=EXE` and `--sil-dut mqtt=EXE` run the firmware itself as the DUT in the
SIL tier: `zephyr.exe` of a `native_sim/native/64` build on a TAP interface (`zeth`) in netns
`lan-a`, with the SIL MAC `02:48:49:4c:00:0a` (`--mac-addr`), hwinfo device id
`0x48494c31` (`--device_id`; MQTT client id `z914koc8`), a flash file under the artifacts
and the console on stdio. The bench is `host/bench-sil.yml` with that board, MAC and client
id. `$HIL_SIL_DUT` takes the same specs separated by `;`. Build the DUT with an extra
configuration fragment:

```
# Zephyr's IP stack on the TAP instead of NSOS; a fixed MAC (else --mac-addr is not offered)
CONFIG_NET_SOCKETS_OFFLOAD=n
CONFIG_NET_NATIVE_OFFLOADED_SOCKETS=n
CONFIG_ETH_NATIVE_TAP=y
CONFIG_ETH_NATIVE_TAP_RANDOM_MAC=n
CONFIG_NET_L2_ETHERNET=y
CONFIG_NET_DHCPV4=y
# sys_reboot re-executes the image in place instead of exiting
CONFIG_REBOOT=y
CONFIG_NATIVE_SIM_REBOOT=y
```

The MQTT build adds `CONFIG_NET_CONNECTION_MANAGER=y`, `CONFIG_HEAP_MEM_POOL_SIZE=16384` and
the site symbols (`CONFIG_APP_MQTT_BROKER_HOSTNAME="broker.hil.lan"`, the test CA as
`CONFIG_APP_MQTT_TLS_CA_CERT_FILE`, and for mTLS the client certificate and key).

A DUT process that ends by itself is started again, as hardware comes back after a reset.
MQTT-01..03, TLS-01, TLS-03, NET-03, BIP-01, BIP-03 and PERS-01 run against it; timing
criteria and anything needing the stimulus board skip. The BACnet tests need
`$HIL_BACNET_HARNESS` (SMP) as on hardware.

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
MYPYPATH=tests/unit:tests/mstp mypy --strict tests/conftest.py tests/unit tests/rig tests/system \
  tests/reset tests/power tests/mqtt tests/tls tests/net tests/bacnet_ip tests/io \
  tests/persistence tests/release
MYPYPATH=tests/unit:tests/mstp mypy --strict tests/sil
MYPYPATH=tests/unit:tests/mstp mypy --strict tests/mstp
```

The test directories are type-checked in three runs because mypy cannot hold two modules
named `conftest` at once.

Two runs on one host share the namespace names (`hu-`, `hn-`, the rig's). To run suites
side by side, give each its own `/run/netns` and network namespace:
`unshare --mount --net --propagation private sh -c 'mkdir -p /run/netns && mount -t tmpfs
tmpfs /run/netns && ip link set lo up && exec pytest --sil'`. A docker broker cannot join
such a run, because dockerd resolves `/run/netns/svc` in the host's view: run isolated
suites where docker is not running. The broker then runs locally without a key log, and the
tests that decrypt its TLS skip with that reason.

The sigrok decoder (`decoders/`) and the Logic 2 extension (`saleae/`) are checked as
Python 3.8 code in their hosts' style. Every fix made during integration has a regression
test that names the defect in its docstring (`Regression: ...`).

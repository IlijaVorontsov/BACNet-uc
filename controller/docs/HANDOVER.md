# Handover: uc-controller (Raspberry Pi 4 site controller)

Date: 2026-09-25. Branch: `claude/bacnet-mqtt-broker-devices-57orta` of
`IlijaVorontsov/BACNet-uc`.

This note is for the colleague who takes over the site controller. You have
already run Linux-based controllers in Rust. The design here was written with
Python for our own services, but every boundary that matters is a file
format, a protocol or an HTTP API. You can build any of the components in
Rust without changing the other components or the other teams' code.
Section 6 lists what a Rust implementation must keep.

## 1. Summary

- **What it is.** One Linux box per building on a Raspberry Pi 4 Model B
  (4 GB) that boots from a USB 3.0 SSD. It runs:
  - the MQTT broker for the building's MQTT devices;
  - uc-hub, the building gateway with the AI agent;
  - a time-series database for trends, device logs and events;
  - NTP time for the devices;
  - the site certificate authority (CA);
  - backups and health monitoring.

  Control logic stays on the field controllers, so a controller outage costs
  supervision and history, never control.
- **State.**
  - The design is finished: [`DESIGN.md`](DESIGN.md), 2242 lines, commit
    `ccd8dde`.
  - Implementation was stopped partway through the first of seven work
    packages (WP1), at the user's request. That partial code is committed as
    WIP (commit `2e9a317`). It has not been reviewed.
  - Nothing has run on a real Pi.
- **Start here:**
  1. This file.
  2. `DESIGN.md` §1 (rules), §2 (scope and failure behaviour), §6
     (network), §10 (history).
  3. `DESIGN.md` §21–§23 (requests to other teams, risks, work packages).

## 2. Project context

BACnet-uc is a building-automation platform. Several parallel work streams
each have their own branch. They coordinate through `docs/SESSION_NOTES.md`,
where each branch records its decisions and the requests it makes to the
others. Read another branch's notes with
`git show origin/<branch>:docs/SESSION_NOTES.md`.

| Branch | Content | Relation to the controller |
|---|---|---|
| `claude/zephyr-bacnet-stm32-162k1g` | BACnet firmware (Zephyr 4.4.2, NUCLEO-F767ZI and FRDM-MCXN947). WASM control apps, LittleFS, SMP management on UDP 1337, optional syslog on UDP 514. Docs in `docs/` (`management-protocol.md`, `security.md`, `storage-and-logging.md`). | The controller reaches these nodes over BACnet/IP and SMP, and receives their syslog. |
| `claude/inter-session-communication-h989ye` | `apps/mqtt_tls`, the MQTT client firmware, now at fw 0.4.0: MQTT 3.1.1 over TLS 1.3/1.2 with mutual TLS, OTA through MCUboot and SMP, runtime configuration, logs over MQTT. | The controller runs their broker, issues their certificates and archives their messages. |
| `claude/ai-harness-building-control-05nrgm` | `hub/` (uc-hub: Python 3.12, FastAPI, aiosqlite, aiomqtt, bacpypes3, MCP), `web/`, `docs/ai-harness/`. Drivers for BACnet-uc (SMP), third-party BACnet/IP and MQTT. | The controller hosts the hub. Today the hub keeps trend history only in an in-memory buffer; the controller adds a durable store behind an HTTP API (requests C1–C21). |
| `claude/hardware-in-loop-testing-x74tww` | HIL test rig (`hil/`, `docs/HIL.md`). Its contract FW-01..FW-12 binds both firmware apps. | The rig may later run a real controller (requests HIL-C1/C2). |
| **`claude/bacnet-mqtt-broker-devices-57orta`** (this branch) | `controller/` (design + WIP code). `docs/SESSION_NOTES.md` holds our requests M4–M7 to the MQTT firmware; M4–M6 are done. | – |

Those branches are owned by separate sessions. Don't edit their code; write
requests into `docs/SESSION_NOTES.md` on this branch instead.

## 3. Hardware

The user has a **Raspberry Pi 4 Model B, 4 GB** and an SSD on USB 3.0. The
full BOM with reasons is in DESIGN.md §3. The points that decide whether the
box survives in a cabinet:

| Item | Recommendation | Why |
|---|---|---|
| SSD | 2.5" SATA with power-loss protection, e.g. Kingston DC600M 480 GB | Protects the PostgreSQL WAL and ext4 across power cuts. It fits the Pi 4's 1.2 A USB power budget. |
| USB-SATA bridge | ASMedia ASM1153E or ASM235CM, UAS, blue USB 3 port. Avoid JMicron JMS567/578. | UAS and TRIM behaviour. Test every purchase batch (DESIGN.md §20, tier T5). |
| RTC | DS3231 or RV-3028 module (the Pi 4 has no clock) | The controller is the site's time source and stamps every reading. |
| Power | Official 5.1 V / 3 A supply, or 24 V DIN rail to a 5.1 V / ≥3 A DC/DC | Under-voltage is the most common Pi failure. |
| Network | Onboard port with two 802.1Q VLANs (IT and BMS), or a USB GbE NIC for a physically separate BMS network | Both are supported (`network.mode`: `trunk`, `dual`, `flat`). |

Pi 4 specifics that affect the software:
- The Cortex-A72 has **no ARMv8 crypto extensions**, so AES is slow. Prefer
  ECDSA P-256 and ChaCha20-Poly1305 where you have the choice. This matters
  for TLS stacks and for LUKS (Adiantum instead of AES-XTS).
- Boot from USB needs `BOOT_ORDER=0xf14` in the bootloader EEPROM (USB
  first, then SD). See DESIGN.md §4.4.

## 4. Architecture in one page

```
                IT LAN (it0)                                BMS network (bms0)
  browsers ── HTTPS 443 ──┐                   ┌── MQTT/TLS 8883 ── mqtt_tls nodes (mTLS, CN = client id)
  admins ──── SSH 22 ─────┤                   ├── BACnet/IP 47808 ── BACnet-uc nodes, third-party controllers
  upstream NTP, backups ──┤                   ├── SMP UDP 1337 ────── BACnet-uc and mqtt_tls nodes (management, OTA)
                          │                   ├── syslog UDP 514 ←── BACnet-uc nodes
                          │                   └── NTP 123, optional DHCP/DNS ── all devices
                ┌─────────▼───────────────────▼─────────────────────────────────────┐
                │ nginx ── uc-hub (127.0.0.1:8080; Python, harness session's code)   │
                │            │  POST /v1/readings, PUT /v1/points, POST /v1/events    │
                │            ▼  (HTTP over a Unix socket)                             │
                │ uc-historian ── PostgreSQL 18 + TimescaleDB (Unix socket only)      │
                │      ▲ persistent MQTT session, ack after commit                    │
                │ mosquitto 2.1 (8883 mTLS for devices, 127.0.0.1:1883 for services)  │
                │ chrony (RTC-backed site time), nftables, uc-ctl, uc-health, backups │
                └───────────────────────────────────────────────────────────────────┘
```

The rules everything else follows (DESIGN.md §1):
- **R1:** the plant never depends on the controller.
- **R4:** two failure domains on one SSD. Root holds supervision; `/srv/uc`
  holds history and is mounted `nofail`.
- **R7:** a power cut is a normal event.
- **R8:** offline first; no Internet is required.
- **R9:** everything is bounded.
- **R10:** configuration changes are transactional, with rollback.
- **R11:** one writer per kind of data (only the historian writes the
  database).

## 5. What exists, and what does not

| Area | Status |
|---|---|
| Design (`controller/docs/DESIGN.md`) | Done. It was built from five research reports and three competing designs, and covers every area. Claims are tagged [V] verified in a container or VM, [R] read in docs, [U] unverified, [HW] needs real hardware. |
| WP1: `uc-ctl` core, site schema, network/firewall, SSH, time, DHCP roles | **Partial, unreviewed** (commit `2e9a317`). See below. |
| WP2: broker, PKI, device registry | not started (spec in DESIGN.md §8, §9, §23) |
| WP3: historian + PostgreSQL/TimescaleDB | not started (§10) |
| WP4: uc-hub hosting (deb with venv), nginx | not started (§11, §12) |
| WP5: health, backup/restore, update, runbooks | not started (§14–§17) |
| WP6: OS/boot roles, first boot, partitioning, golden image | not started (§4, §5.5) |
| WP7: end-to-end test on arm64 Pi OS | not started (§20, §23) |
| Hub changes C1–C21 | Requests only; the harness session has not been told yet. See §7. |

WP1 as it stands (`controller/src/uc_controller`, about 3700 lines):
- Written:
  - `site/controller.schema.json` and three site examples (trunk, dual,
    flat).
  - The config loader with derived values.
  - The role interface (`roles/base.py`, DESIGN.md §19.2).
  - The renderer (Jinja2, deterministic, staging prefix).
  - The validator runner.
  - The apply transaction (`apply.py`: atomic writes, LKG snapshots,
    rollback, confirm timer).
  - The secret store.
  - The network (systemd-networkd + nftables), ssh, time (chrony) and dhcp
    (dnsmasq) roles with their templates.
  - The pinned third-party apt repos (`packaging/repos/`).
  - The container test helpers (`tests/lib/ctr.sh`, Dockerfiles).
- Verified: `tests/core` has 59 host unit tests (schema, derived values,
  rendering), and they pass on Python 3.12.
- Not done:
  - review;
  - the container and arm64 tiers of the WP1 tests (for example `nft -c`,
    `sshd -t`, `chronyd -p` against the rendered files);
  - `tests/lib/import-raspios.sh`;
  - tests for the apply transaction;
  - parts of the WP1 spec that the build agent had not reached. The full WP1
    spec is in DESIGN.md §5.1–§5.4, §6, §7, §19.

Treat it as a draft that follows the interfaces of DESIGN.md §19.

## 6. Contracts to keep, and where Rust fits

Our own services were planned as Python using only Debian-packaged
libraries: no pip and no containers on the box, and each component ships as
a `.deb` (rule R3). A Rust binary built with `cargo-deb` fits that rule just
as well, and probably better for the long-running services. The contracts
below don't depend on the language.

### 6.1 Contracts

| Contract | Defined in | Consumers |
|---|---|---|
| Site file `controller.yaml` (no secrets; lives in the integrator's git) | `controller/site/controller.schema.json`, DESIGN.md §5.1 | `uc-ctl`, integrators |
| Historian HTTP API v1 on `/run/uc-historian/api.sock`: ingest `POST /v1/readings`, `PUT /v1/points`, `POST /v1/events`; queries `GET /v1/series`, `/v1/stats`, `/v1/logs`, `/v1/events`, `/v1/catalog`, `/v1/health` | DESIGN.md §10.7–§10.15 (a separate `HISTORIAN-API.md` was planned in WP3) | uc-hub (requests C2–C9), uc-health, `uc-ctl` |
| Database schema `hist` (hypertables `sample`, `device_log`, `event`; point registry; rollups `sample_1h`/`sample_1d`) | DESIGN.md §10.4–§10.5 | historian only (R11) |
| MQTT topics `<root>/<id>/{status,info,telemetry,cmd,event,log}` and the client ID `z` + base32(UID) | `apps/mqtt_tls/README.md` and `docs/SESSION_NOTES.md` on `claude/inter-session-communication-h989ye` | broker ACL, historian, hub |
| Broker ACL: a device may only write its own `status/info/telemetry/event/log` and read its own `cmd`. The certificate CN is the client ID. | DESIGN.md §8.3 | mosquitto |
| SMP management (UDP 1337, CBOR, MCUboot image upload) | `docs/management-protocol.md` on the BACnet branch | hub (and later OTA tooling) |
| Hub datatypes `real`, `int`, `enum`, `bool`, `string` | `hub/src/uc_hub/core/types.py` on the harness branch | historian schema and API |

### 6.2 Invariants any implementation must keep

- **Ack after commit.** The historian subscribes with a persistent session
  (`clean_session=false`, QoS 1, fixed client ID `uc-historian`). It
  acknowledges a message only after the database transaction has committed.
  While PostgreSQL is down it stops acknowledging, and the broker queues up
  to 200 000 messages. With paho 2.1 this was verified [V]. In Rust, rumqttc
  offers manual acks (`set_manual_acks`); verify redelivery with DUP=1 after
  an unclean reconnect against mosquitto 2.1.2 before relying on it.
- **Idempotent ingest.** Use one statement per batch (`unnest` arrays with
  `ON CONFLICT DO NOTHING` on `(point_id, ts)`). This works on compressed
  TimescaleDB chunks [V]. The hub retries batches, so duplicates must be
  harmless.
- **Bounded everything.** Request size limits (≤ 5000 readings or 1 MiB),
  query limits (≤ 1000 buckets, ≤ 10 years, ≤ 500 log lines), per-source
  syslog token buckets (50/s, burst 500), per-unit `MemoryMax`
  (historian 256 MB).
- **Tier routing and time-weighted aggregation** for `/v1/series` (DESIGN.md
  §10.15). Use LOCF time-weighted averages, never the arithmetic mean of
  change-driven samples. Report coverage and gaps.
- **Device text is data.** Log lines and MQTT payloads come from devices and
  can contain anything, including text that looks like instructions to the
  AI agent. Store them with parameterised SQL, cap their length, strip
  control characters, and never interpret them.
- **systemd integration:** `Type=notify` with `WATCHDOG=1` from the main
  loop (the `sd-notify` crate in Rust), `Restart=always`, the hardening
  options listed in DESIGN.md §10.6 and §11.3, and exposure ≤ 2.5 in
  `systemd-analyze security`.
- **Target:** `aarch64-unknown-linux-gnu`, Debian 13 trixie (glibc 2.41).
  Build against trixie's glibc or older, or link statically.

### 6.3 Suggested split if you move to Rust

| Component | Planned in | Rust fit |
|---|---|---|
| `uc-historian` (MQTT archive, syslog receiver, ingest and query API, storage policy) | Python (asyncpg, aiohttp, paho) | **Best candidate.** It is long-running, memory-bounded and performance-sensitive on a Pi 4. It is self-contained behind the §10 API. |
| `uc-health` (probes, alarms, MQTT health JSON) | Python | Good candidate; small. |
| `uc-ctl` (render/validate/apply/rollback of system configs) | Python (WP1 draft exists) | Possible, but the value is in the templates and the transaction logic, not the language. Keep the rendered file formats and the `controller.yaml` schema. |
| `uc-pki` (site CA) | bash + openssl 3.5 | Keep it in openssl unless you have a reason; it's easy to audit. |
| uc-hub | Python, owned by the harness session | Not ours to change. The controller only packages and hosts it. |

## 7. Requests to other teams (not yet sent, except where noted)

The full tables are in DESIGN.md §21.
- **Harness session (uc-hub), C1–C21.** The most important are:
  - C1: SIGTERM should run the hub's stop path. This is a verified defect.
  - C2: a history sink that pushes readings to `/v1/readings`.
  - C3: push the point registry.
  - C5: back `point_history` with `/v1/series`.
  - C9: a `history:` key in `hub.yaml`.
  - C12: secrets from files.
  - C14: dev mode must not be reachable through the nginx proxy on the same
    host.

  Until C2/C3 land, the historian still archives MQTT status, info, event
  and log messages and syslog, but point trends only exist in the hub's
  memory.
- **MQTT firmware.** M4–M6 are done (fw 0.4.0). M7 (SNTP and timestamps) was
  deferred by the user. M8–M10 (log the client ID at boot, a boot ID and
  sequence numbers in logs, a TLS date tolerance before time sync) are
  proposed in DESIGN.md §21.
- **BACnet firmware.** B2 `lease_ms` is a hard dependency of rule R1:
  without it, forced outputs stay forced while the controller is down.
- **HIL rig.** HIL-C1 asks for a controller bench slot and HIL-C2 for the
  controller to serve broker, NTP and DHCP to the rig.

## 8. Open decisions with the user

Nobody has answered these yet; the design uses the defaults in brackets.
1. The TimescaleDB Community edition is under the TSL licence, not an
   open-source one. Is that acceptable for per-site installs by the
   integrator? [assumed yes] The fallback is plain PostgreSQL behind the
   same API, which fits about 90 days of raw history instead of about 400.
2. Retention: raw 400 days, hourly and daily rollups forever, device logs
   180 days, events 10 years?
3. Default network per customer: a VLAN trunk on the onboard port, or a
   second USB NIC? [trunk; all three modes are supported]
4. Is the RTC module mandatory on every controller? [yes]
5. Who holds the site root CA key (integrator, owner, both)? Do owners
   provide SFTP backup targets and upstream NTP?
6. Is a fleet update service with an A/B root wanted in v2, or are USB/SSH
   updates enough?

## 9. Reproducing the test environment

Everything was prepared in an x86-64 container. Nothing here needs a Pi
except tier T5.

```sh
# arm64 emulation for docker (once per host)
docker run --privileged --rm tonistiigi/binfmt --install arm64

# official Raspberry Pi OS Lite image (Debian 13.7, kernel 6.18.50, systemd 257)
b=https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-09-15/2026-09-15-raspios-trixie-arm64-lite
curl -LO $b.img.xz -LO $b.img.xz.sha256 && sha256sum -c *.sha256 && xz -dk *.img.xz

# import its root filesystem (partition 2 starts at sector 1064960) as an arm64 docker image
mkdir mnt && mount -o loop,ro,offset=$((1064960*512)),sizelimit=$((4915200*512)) \
  2026-09-15-raspios-trixie-arm64-lite.img mnt
tar -C mnt --numeric-owner -c . | docker import --platform linux/arm64 - raspios-lite:2026-09-15
# the FAT boot partition (sector 16384) can be read without mounting:
MTOOLS_SKIP_CHECK=1 mcopy -s -i "2026-09-15-raspios-trixie-arm64-lite.img@@$((16384*512))" ::/ bootfs/
```

Facts checked in that image:
- mosquitto 2.0.21, PostgreSQL 17 and chrony 4.6.1 are available from
  Debian.
- The design pins mosquitto 2.1.2 from repo.mosquitto.org, and PostgreSQL 18
  and TimescaleDB 2.30 from PGDG and packagecloud.
- The image ships cloud-init, NetworkManager and systemd-timesyncd. The
  design replaces them with its own first boot, systemd-networkd and chrony.

Test tiers (DESIGN.md §20):

| Tier | What |
|---|---|
| T0 | static checks |
| T1 | unit tests |
| T2 | install and validate in the arm64 Pi OS container |
| T3 | daemons in containers |
| T4 | full-system `qemu-system-aarch64 -M virt`; a boot to multi-user was verified at about 4 min |
| T5 | real Pi 4 acceptance, including 50 power pulls per SSD/bridge batch |

systemd does not run as PID 1 under qemu-user, so unit behaviour needs T4.

Run the existing WP1 unit tests:

```sh
python3 -m venv .venv && .venv/bin/pip install pyyaml jinja2 jsonschema pytest
UC_CONTROLLER_SRC=$PWD/controller PYTHONPATH=controller/src .venv/bin/python -m pytest -q controller/tests/core
```

## 10. Suggested next steps

1. Decide Python or Rust for each of our own components (§6.3). The
   contracts don't change either way.
2. Finish WP1, or replace it. The minimum needed to bring up a Pi is the
   site schema, network/firewall and time.
3. Build WP2 (broker + PKI) and WP3 (historian) next. Together they give the
   first useful result: MQTT devices with per-device certificates, and their
   status, logs and events archived and queryable.
4. Send the C-requests (§7) to the harness session through
   `docs/SESSION_NOTES.md`, starting with C1, C2, C3 and C9.
5. Bring up the user's Pi 4 early (lab path in DESIGN.md §4.1: stock Pi OS
   Lite on the SSD, then the installer) and run the T5 checks on the actual
   SSD and bridge: `lsusb -t` shows `uas` at 5000M, TRIM, SMART through the
   bridge, `vcgencmd get_throttled`.

## 11. Files

| Path | Content |
|---|---|
| `controller/docs/DESIGN.md` | full design (source of truth) |
| `controller/docs/HANDOVER.md` | this note |
| `controller/site/` | `controller.yaml` schema and examples (WP1 draft) |
| `controller/src/uc_controller/` | `uc-ctl` package (WP1 draft) |
| `controller/templates/` | rendered config templates (network, ssh, time, dhcp) |
| `controller/rootfs/` | static files installed by the package (time policy helper, unit drop-ins, firewall fail-safe) |
| `controller/packaging/repos/` | pinned third-party apt repositories and keys |
| `controller/tests/` | test helpers and WP1 unit tests |
| `docs/SESSION_NOTES.md` | cross-team notes; our requests M4–M7 to the MQTT firmware |

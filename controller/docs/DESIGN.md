# uc-controller: design

Status: design v1, 2026-09-25. Nothing below has run on real Pi 4 hardware yet.

uc-controller is the per-site Linux box of the BACnet-uc project. It runs the
MQTT broker for the `mqtt_tls` nodes, hosts uc-hub, keeps trends, device logs
and events in a time-series database, serves NTP (and optionally DHCP/DNS) to
the devices, holds the site PKI, and backs up and monitors itself. Control
logic stays on the boards (WASM apps, leased writes). A controller outage
costs supervision and history, never control.

Target platform:

| Item | Value |
|---|---|
| Board | Raspberry Pi 4 Model B, 4 GB (8 GB optional) |
| Storage | 2.5" SATA SSD with power-loss protection, on a USB 3.0 UAS bridge, boot and data on the same SSD |
| OS | Raspberry Pi OS Lite arm64 2026-09-15 = Debian 13.7 "trixie", kernel 6.18.50+rpt-rpi-v8, systemd 257.13, OpenSSL 3.5.7, Python 3.13.5 |

This design merges three candidate designs (appliance-first, fleet-first,
data-first) and five research reports. The appliance design is the base. The
data design supplies the history store, the historian and the hub contract.
The fleet design supplies the A/B update roadmap, the fail-safe firewall,
the migration rules for rollback and the reproducibility checks. Appendix A
lists every conflict and how it was resolved.

Evidence tags:

- **[V]** verified in arm64 containers or a qemu-system VM during research or design.
- **[R]** read in vendor documentation or source during research.
- **[U]** unverified. The text names the test that settles it.
- **[HW]** can only be settled on real hardware (test tier T5, §20).

---

## 1. Design rules

| # | Rule | Consequence |
|---|---|---|
| R1 | **The plant never depends on the controller.** | Nothing on the controller may make a board wait for it. Live writes need B2 `lease_ms` on the BACnet firmware (§21). |
| R2 | **One image, one site file, one bundle.** | The same image runs at every site. `controller.yaml` holds everything site-specific and contains no secrets, so it can live in the integrator's git. Secrets and state live on the box and in one encrypted site bundle. Install and replace use the same steps. |
| R3 | **Native packages, few repos.** | Debian trixie and archive.raspberrypi.com, plus three pinned third-party repos with a stated reason each: PGDG (PostgreSQL 18, pgBackRest), packagecloud Timescale (TimescaleDB, toolkit), repo.mosquitto.org (mosquitto 2.1.2). Our code ships as two .debs. No containers and no pip on the box. |
| R4 | **Two failure domains on one SSD.** | Root (p2) holds supervision: mosquitto, uc-hub and its SQLite DB, chrony, dnsmasq, nginx, sshd, journald. Data (p3, `/srv/uc`) holds history: PostgreSQL, the historian, backups. p3 is mounted `nofail`. If p3 is lost, only history is lost. |
| R5 | **Everything is supervised.** | `Restart=always` on every service. Hardware watchdog for PID 1. Storage liveness for a vanished SSD. Functional probes restart hung daemons at most 3 times per hour. |
| R6 | **Self-healing is bounded.** | At most 3 unclean boots in 24 h trigger automatic reboots. After that the box stays up degraded and raises an alarm. |
| R7 | **A power cut is a normal event.** | ext4 journal, PostgreSQL WAL and data checksums, an SSD with power-loss protection, the FAT boot partition read-only at runtime, MQTT messages acknowledged only after the database commit. |
| R8 | **Offline first.** | No Internet or IT service is required. RTC-backed NTP, local DHCP/DNS optional, all TLS local. Updates and restores work from files. |
| R9 | **Everything is bounded.** | Disk budgets, queue sizes, memory limits per unit, explicit OOM order, query limits. |
| R10 | **Configuration changes are transactional.** | Render, validate with each daemon's own checker, install atomically, probe, roll back automatically. Network, firewall and SSH changes revert unless confirmed within 180 s. |
| R11 | **One writer per kind of data.** | The historian is the only database writer. The hub never touches the database; it talks to a versioned HTTP API on a Unix socket. |

---

## 2. Scope, ownership and failure behaviour

**The controller does:**
- run mosquitto: 8883 mutual TLS for devices, 127.0.0.1:1883 for local services;
- host uc-hub behind an HTTPS reverse proxy (BACnet/IP, SMP, MQTT drivers);
- store trends, device logs and events (PostgreSQL 18 + TimescaleDB), through the historian;
- receive device syslog (UDP 514) from BACnet-uc nodes;
- serve NTP to the BMS network, and optionally DHCP/DNS;
- hold the site issuing CA, the CRL and the device registry;
- back up, restore and monitor itself.

**It does not:** run control logic, build firmware, run a native_sim farm
(the BACnet firmware's WAMR glue does not build on aarch64 [V]), route between
IT and BMS, or hold firmware/app signing keys. `web/dist`, `uc-link.wasm`,
app `.wasm` files and signed firmware images are CI artifacts.

**Ownership between sessions:**

| Area | Controller session (`controller/`) | Harness session (`hub/`, `web/`) | Firmware sessions |
|---|---|---|---|
| OS, storage, network, firewall, broker, PKI, NTP/DHCP, proxy, backup, monitoring, updates | owns | – | – |
| Database schema, retention, rollups, historian, query API | owns | consumer | – |
| Point readings (SMP, BACnet/IP, decoded MQTT) | stores | produces and pushes (C2) | – |
| Point and device metadata | mirrors | authoritative, pushes (C3) | B3/M1 hwid, mac |
| Device logs | ingests MQTT (M6) and syslog | reads through the API (C6) | M6, M9, syslog |
| Audit | mirrors into the timeline | authoritative (hub DB) | – |
| uc-hub packaging, unit, rendered `hub.yaml` | owns | code changes (C1–C20) | – |

**Degradation matrix:**

| Fault | Plant | Supervision | History | Automatic reaction |
|---|---|---|---|---|
| Controller off or dead | runs; leases expire on the boards | lost | gap | none; the external dead-man check on `/healthz` alarms |
| Power cut, power returns | runs | back in about 60–90 s [HW] | gap; at most about 0.6 s of acknowledged hub readings lost (§10.14) | ext4 journal replay, PG crash recovery, broker reloads its SQLite store |
| Data partition corrupt or missing | runs | **up** | down | p3 is `nofail`; PG and historian do not start; hub keeps its in-memory history; alarm |
| SSD drops off the USB bus | runs | lost | lost | storage liveness forces `reboot-immediate`; after 3 unclean boots in 24 h reboots stop and the box alarms |
| PostgreSQL down | runs | up | hub buffers in RAM; broker queues MQTT logs/status/events (200 000 messages) | systemd restart; alarm after 5 min |
| Historian crash | runs | up | hub retries; broker redelivers unacknowledged messages; syslog lost while down | `Restart=always`; gap event from the heartbeat |
| mosquitto hung | runs | MQTT devices unsupervised | MQTT gap | probe restarts it (≤3 per hour) |
| uc-hub hung or crash-looping | runs | UI and agent lost | point samples stop; logs, status, events continue | `Restart=always`; probe restart; alarm |
| Upstream NTP lost | runs | up | timestamps correct (RTC) | chrony serves from its local reference; warning after 30 min |
| RTC invalid and no upstream at boot | runs | up | timestamps may be wrong | chrony does not serve until synchronised; alarm; `uc-ctl time set` |
| Bad `controller.yaml` edit | runs | unchanged | unchanged | validation refuses it, or automatic rollback to the last known good set |
| Bad package update | runs | possibly down | – | `uc-ctl rollback` reinstalls the previous uc-* debs and restores snapshots |

---

## 3. Hardware and bill of materials (per site)

| # | Item | Decision | Why |
|---|---|---|---|
| 1 | Board | Raspberry Pi 4 Model B **4 GB**, board revision ≥ 1.2. 8 GB if the site has more than about 3000 trended points or budget allows. Not 1/2/3 GB. | Memory budget §4.8. In production until at least January 2034 [R]. |
| 2 | SSD | 2.5" SATA **with hardware power-loss protection**: Kingston DC600M 480 GB (1.3 W idle, 3.6 W peak write, 876 TBW, 0–70 °C) [R]. At least 240 GB. | PLP protects the WAL and ext4 across power cuts. Peak about 0.72 A at 5 V fits the Pi 4's 1.2 A total USB budget. |
| 3 | USB bridge | USB 3.0 to SATA on **ASMedia ASM1153E** (firmware 141126a1ee82 or later) or **ASM235CM**, UAS, short cable, blue USB 3 port. **Never JMS567/JMS578.** Buy spares from the same lot. | The rpi-6.18 kernel detects ASM1153 correctly and keeps UAS on; SMART works with `-d sat` [R]. Vendors change chips silently, so every batch passes acceptance (§20 T5). |
| 4 | RTC | **Mandatory.** DS3231 I2C module or HAT (TCXO) with a CR2032 and no charging circuit on a primary cell, or RV-3028 (`backup-switchover-mode=1`). Isolated sites with sky view: Uputronics GPS/RTC HAT. | The Pi 4 has no RTC. The controller is the site NTP source and stamps every reading. |
| 5 | Power | Bench: official 5.1 V / 3 A USB-C supply. Cabinet: 24 V DIN rail to a 5.1–5.2 V / ≥3 A DC/DC with a short, thick USB-C lead. Optional DC-UPS with an "on battery" contact to GPIO17 that **cycles its output** after shutdown. | Under-voltage is the most common Pi failure. With `WAKE_ON_GPIO=1` a halted Pi only restarts on a power cycle. |
| 6 | Enclosure | Passive aluminium heatsink case or DIN enclosure. SSD mounted beside the board. Fan only above about 40 °C cabinet temperature. | Rated 0–50 °C ambient; throttling from 80 °C [R]. |
| 7 | Service kit, per technician | (a) SD card with the Imager image "Bootloader (Pi 4) → restore"; (b) rescue SD with stock Pi OS Lite of the same release; (c) 3.3 V USB-UART cable; (d) optional USB stick labelled `UCBACKUP`. **Nothing inserted during operation.** | EEPROM recovery; field access if the SSD is dead. |
| 8 | Cold spare, per region | One Pi 4 with the EEPROM already configured, plus an RTC module. | Board swap in about 15 min (§14.4). |
| 9 | Option `dual` | RTL8153 (r8152) or AX88179 USB GbE NIC for a physically separate BMS network. Plug into a USB 2.0 port. | BSI INF.14.A28. Shares the VL805 controller and the 1.2 A budget with the SSD. |

The SSD choice decides crash safety. Power-loss protection plus a bridge that
passes SYNCHRONIZE CACHE is what protects the database; a UPS only keeps
supervision up.

---

## 4. OS, boot chain and storage

### 4.1 Base image

- Base: `2026-09-15-raspios-trixie-arm64-lite.img.xz`, sha256 pinned in
  `controller/image/base.lock`. Not Debian's own raspi images: rpi-eeprom,
  vcgencmd, the overlays and the watchdog and TRIM plumbing exist only in
  Pi OS [R].
- **Golden image** (`controller/image/build.sh`, rootless, no loop devices [V]):
  1. Extract p1 (FAT) with mtools and p2 (ext4) with `debugfs rdump` from the
     official image. The rootfs alone does not carry the FAT contents [V].
  2. `docker import` p2 as an arm64 rootfs, with p1 copied into
     `/boot/firmware` so kernel hooks keep it consistent.
  3. Inside that arm64 container: add the three third-party repos with pinned
     keys and apt pins (§4.10), write
     `/etc/postgresql-common/createcluster.d/uc.conf` with
     `create_main_cluster = false`, then `apt-get install` the pinned package set
     plus `uc-controller_*.deb` and `uc-hub_*.deb` from a local file repo.
  4. Purge and mask the list in §4.9. Rewrite apt sources to `https://`.
  5. Write `config.txt`, `cmdline.txt` and `fstab` (§4.3–4.5). Enable our units.
     Stamp `/etc/uc-controller/release`.
  6. Clamp mtimes to `SOURCE_DATE_EPOCH`, drop apt lists and caches, empty
     `/etc/machine-id`, delete SSH host keys. Fail the build if the build-time
     proxy CA is found anywhere in the tree.
  7. Assemble an MBR disk: p1 FAT32 512 MiB (`mkfs.vfat --invariant` +
     `mcopy`), p2 ext4 8 GiB (`mkfs.ext4 -d`, label `ucroot`). Fixed disk id per
     release. Output `.img.xz`, sha256, SBOM (dpkg list + uc-hub venv RECORD
     files), file manifest.
- **Lab path** (development and first units before the image exists): stock Pi
  OS Lite written with Imager, then `controller/packaging/install-lab.sh`
  (adds repos and pins, installs the two debs) and `uc-ctl apply`. Same code as
  the image.
- A/B root with RAUC and Pi tryboot is v2 (§17.5), after T5 has shown tryboot
  works from a USB SSD.

### 4.2 Partition layout (MBR)

| Part | Size | FS | Mount | Contents |
|---|---|---|---|---|
| p1 | 512 MiB | FAT32 | `/boot/firmware`, **read-only** at runtime | firmware, kernel, initramfs, overlays, `config.txt`, `cmdline.txt`, first-boot `uc-controller.yaml` |
| p2 | 8 GiB in the image, grown to **32 GiB** at first boot | ext4, label `ucroot` | `/` | OS, `/etc`, `/opt/uc-hub`, `/var/lib/uc-hub` (hub DB), `/var/lib/mosquitto`, journal (≤1 GiB) |
| p3 | rest of the disk minus `controller.ssd_reserve_percent` (default 0 for a PLP drive, 10 for a consumer SSD) | ext4, label `ucdata`, `-m 2` | `/srv/uc` | PostgreSQL, historian state, backup staging, reports |

- `cmdline.txt` has no `resize`; Pi OS never grows p2 on its own.
- First boot grows p2 (`sfdisk -N 2`, then online `resize2fs`) and appends p3
  (`sfdisk --append`, `mkfs.ext4`). Each step is idempotent. A power cut
  between the table write and `resize2fs` leaves a partition larger than its
  filesystem, which is harmless.
- All units of a release share PARTUUIDs. Never attach two controller SSDs to
  one Pi. `uc-ctl selftest` warns about duplicates. Randomising the disk id at
  first boot was rejected: a power cut between the table write and the
  cmdline/fstab rewrite leaves an unbootable unit.

### 4.3 fstab

```
PARTUUID=<id>-01 /boot/firmware vfat ro,noatime,nofail,umask=0077                                     0 2
PARTUUID=<id>-02 /              ext4 defaults,noatime,errors=remount-ro                               0 1
PARTUUID=<id>-03 /srv/uc        ext4 defaults,noatime,nodev,nosuid,errors=remount-ro,nofail,x-systemd.device-timeout=30s 0 2
```

- The image default is `errors=Continue` [R]; `remount-ro` is right for a database.
- No `discard`. `fstrim.timer` is enabled in the image.
- FAT is written only by package operations. `/etc/apt/apt.conf.d/10uc-bootfs-rw`
  remounts it read-write in `DPkg::Pre-Invoke` and read-only in `Post-Invoke`.
  `uc-ctl eeprom apply` and first boot do the same. FAT has no journal, so a
  power cut during a write can make the Pi unbootable.

### 4.4 Bootloader EEPROM

Applied by `uc-ctl eeprom apply` (`rpi-eeprom-config --apply`, one reboot). It is
idempotent and runs at first boot and in maintenance windows only.

```
[all]
BOOT_ORDER=0xf14            # USB first, then SD: a forgotten SD card never replaces production
BOOT_UART=1                 # bootloader log on the cabinet serial console
NET_INSTALL_AT_POWER_ON=0   # no network-install UI on a headless box
NET_INSTALL_ENABLED=0       # key name to confirm with rpi-eeprom-config on the target bootloader [HW]
WAKE_ON_GPIO=1
POWER_OFF_ON_HALT=0
# BOOT_WATCHDOG_TIMEOUT=60  # GATED by T5-7; left out until the test passes
```

- Bootloader ≥ **2026-05-17** (MFG version 1). Never set `FREEZE_VERSION=1`;
  undoing it needs an SD-card recovery [R]. Never run `rpi-update`.
- `rpi-eeprom-update.service` is **masked**, so the bootloader only changes in a
  maintenance window (`uc-ctl eeprom check|apply`).
- Rescue: the SSD is unplugged and the rescue SD boots. EEPROM recovery uses
  the bootloader-restore SD, which the Pi 4 ROM loads whatever `BOOT_ORDER` says.

### 4.5 config.txt and cmdline.txt

Image defaults kept: `arm_64bit=1`, `auto_initramfs=1`, `disable_fw_kms_setup=1`,
`arm_boost=1`, `vc4-kms-v3d`. Added (rendered by the `boot` role):

```
dtparam=audio=off
camera_auto_detect=0
display_auto_detect=0
[all]
dtoverlay=disable-wifi              # wired only: no 2.4 GHz radio next to USB 3 noise
dtoverlay=disable-bt                # also frees the PL011 UART for the console
enable_uart=1
dtparam=i2c_arm=on
dtoverlay=i2c-rtc,ds3231            # from controller.rtc: ds3231 | rv3028,backup-switchover-mode=1 | (none)
dtparam=act_led_trigger=heartbeat   # visible "kernel alive" LED
# dtoverlay=gpio-shutdown,gpio_pin=17,active_low=1   # when controller.ups_gpio is set; never GPIO3 (RTC I2C SCL)
# kernel_watchdog_timeout=<n>       # GATED by T5-7 (firmware behaviour above the ~16 s HW maximum is unverified)
# never: dvfs=1 (PCIe/USB 3 instability), dtparam=pcie=off (disables the USB 3 controller)
```

`cmdline.txt` (one line):
```
console=serial0,115200 console=tty1 root=PARTUUID=<id>-02 rootfstype=ext4 fsck.repair=yes rootwait
```
`usb-storage.quirks=VID:PID:u` is added only for a bridge that fails UAS in T5.
usb-storage and uas are built into the kernel, so modprobe.d cannot carry it [R].

### 4.6 Watchdog chain, storage liveness, boot gate

1. **Bootloader:** `BOOT_WATCHDOG_TIMEOUT` (gated, T5-7).
2. **Firmware to PID 1:** `kernel_watchdog_timeout` (gated, T5-7). USB
   enumeration plus `rootwait` plus fsck can take longer than the ~16 s hardware
   maximum, and the firmware's handling of larger values is unverified. Until T5
   passes, this gap is accepted.
3. **PID 1:** keep Pi OS's `RuntimeWatchdogSec=1m` and `RebootWatchdogSec=2m`
   (bcm2835_wdt; the watchdog core extends the ~16 s hardware limit) [R].
4. **Panics:** `kernel.panic=10`, `kernel.panic_on_oops=1`. The kernel is built with
   `PANIC_TIMEOUT=0` [R].
5. **Storage liveness** (`uc-liveness.service`): `Type=notify`, `WatchdogSec=90s`,
   `Restart=no`, `FailureAction=reboot-immediate`. Every 10 s it writes and
   fsyncs `/var/lib/uc-controller/.alive` and, when p3 is mounted read-write,
   `/srv/uc/.alive`, then sends `WATCHDOG=1`. A filesystem remounted read-only
   counts as a failure. `reboot-immediate` is used because a forced reboot can
   hang in sync on a vanished disk.
6. **Boot gate** (`uc-boot-gate.service`, early oneshot): appends
   `{boot_id, time, clean, board_serial}` to
   `/var/lib/uc-controller/boot-history.jsonl`. `clean` comes from a marker that
   `uc-shutdown-mark.service` writes in `ExecStop`. With 3 or more unclean boots
   in 24 h it creates `/run/uc-controller/liveness-suppressed`;
   `uc-liveness.service` has `ConditionPathExists=!` on that file and
   `uc-liveness-report.service` (alarm only) runs instead. It also detects a
   new board serial (§14.4).

### 4.7 sysctl, journald, swap, TRIM

- `/usr/lib/sysctl.d/60-uc-controller.conf`:
  `kernel.panic=10`, `kernel.panic_on_oops=1`,
  `vm.dirty_background_bytes=16777216`, `vm.dirty_bytes=67108864`,
  `net.ipv4.ip_forward=0`, `net.ipv6.conf.all.forwarding=0`,
  `net.ipv4.conf.all.accept_redirects=0`, `net.ipv6.conf.all.accept_redirects=0`,
  `net.ipv4.conf.all.send_redirects=0`, `net.ipv4.conf.all.log_martians=1`,
  `kernel.dmesg_restrict=1`, `kernel.kptr_restrict=2`, `net.core.bpf_jit_harden=2`,
  `kernel.sysrq=0`, `fs.suid_dumpable=0`.
  Pi OS's `vm.min_free_kbytes=16384` and rpi-swap's `vm.page-cluster=0` stay.
- `/usr/lib/systemd/journald.conf.d/60-uc-controller.conf` overrides Pi OS's
  `Storage=volatile` [R]: `Storage=persistent`, `SystemMaxUse=1G`,
  `SystemKeepFree=2G`, `MaxRetentionSec=1year`, `SyncIntervalSec=1min`.
- `/etc/rpi/swap.conf.d/60-uc.conf`: `[Main] Mechanism=zram`, `[Zram] RamMultiplier=0.5`.
  PostgreSQL is sized so it never swaps.
- TRIM: `/usr/lib/udev/rules.d/60-uc-usb-ssd.rules` sets
  `provisioning_mode=unmap` and `queue/discard_max_bytes` only for bridge VID:PIDs
  listed in `/usr/lib/uc-controller/bridges.allow` after they pass T5 (LBPU=1).
  The RPi kernel does not enable UNMAP on USB bridges by itself [R].

### 4.8 Memory and OOM policy (4 GB)

| Unit | MemoryHigh / MemoryMax | OOMScoreAdjust | CPUWeight | Class |
|---|---|---|---|---|
| mosquitto | – / 384M | -900 | 200 | supervision |
| chrony, dnsmasq | – / 64M | -900 | 100 | supervision |
| uc-hub | 768M / 1G | -600 | 200 | supervision |
| nginx | – / 128M | -500 | 100 | UI |
| postgresql@18-main | none (Debian unit: postmaster -900, backends 0) | – | 100 | history |
| uc-historian | – / 256M | +300 | 100 | history |
| uc-backup, uc-health | – / 512M, 128M | +800 | 20, `Nice=10` | batch |

Budget [U, measured in T5]: kernel and base 250 MB, PostgreSQL ~750 MB
(`shared_buffers=512MB`), uc-hub 200–350 MB, historian 60–120 MB, mosquitto
≤150 MB, nginx and small daemons ~80 MB, leaving about 2 GB page cache. zram
(2 GB) is a safety net only.

### 4.9 Least functionality

Purged in the image: `rpi-connect-lite`, `cloud-init`, `rpi-cloud-init-mods`,
`userconf-pi`, `avahi-daemon`, `bluez`, `wpasupplicant`, `systemd-timesyncd`
(replaced by chrony), `netplan.io`.
Masked: `NetworkManager`, `NetworkManager-wait-online`, `sshswitch`, `udisks2`,
`rpi-eeprom-update`.
AppArmor stays inactive (Pi OS kernel has `CONFIG_LSM=""` [R]); `lsm=apparmor` is
a later hardening item. In the lab path the `os` role touches
`/etc/cloud/cloud-init.disabled` and masks the same units.

### 4.10 Package sources and pins

| Repo | Packages | Pin |
|---|---|---|
| Debian trixie, trixie-security | base, chrony 4.6.1, dnsmasq 2.91, nginx 1.26.3, nftables 1.1.3, openssh 10.0, age 1.2.1, zstd, sqlite3, smartmontools 7.4, i2c-tools, fake-hwclock 0.14, python3-{yaml,jinja2,jsonschema,asyncpg,aiohttp,paho-mqtt}, unattended-upgrades, needrestart | default |
| archive.raspberrypi.com trixie | kernel, firmware, rpi-eeprom, raspi-utils, OpenSSL/glibc `+rpt` builds | default |
| apt.postgresql.org trixie-pgdg | postgresql-18 (18.6), pgbackrest (2.59.1) | 500 for these; never `postgresql-*-timescaledb` |
| packagecloud.io/timescale/timescaledb trixie | timescaledb-2-postgresql-18 (2.30.1), -loader, timescaledb-toolkit-postgresql-18 (1.26.0) | 900 |
| repo.mosquitto.org trixie | mosquitto, mosquitto-clients, libmosquitto1 (2.1.2) | 900 for these three, 100 for everything else |

`/etc/apt/preferences.d/uc-controller.pref` also sets `Pin-Priority: -1` for
`postgresql-18-timescaledb`, `postgresql-17-timescaledb` and
`timescaledb-2-oss-postgresql-18`. The `+dfsg` builds from Debian and PGDG are
Apache-only and must never replace the TSL build (TimescaleDB issue #7787: an
edition flip returned partial data) [R]. Repo definitions, keys (with their
fingerprints) and pins are committed under `controller/packaging/repos/` and
installed by `controller/packaging/repos/add-repos.sh`, which the image build,
the lab installer and the test containers all use.

---

## 5. Provisioning and configuration

### 5.1 `controller.yaml` (site file, no secrets)

Read from `/boot/firmware/uc-controller.yaml` at first boot, then kept at
`/etc/uc-controller/controller.yaml`. Validated against
`controller/site/controller.schema.json` (JSON Schema draft 2020-12,
`additionalProperties: false` everywhere). The name avoids a clash with the
hub's `site.yaml` building manifest.

```yaml
version: 1
site:
  id: hq                        # ^[a-z0-9][a-z0-9-]{0,62}$; must equal the hub site.yaml metadata.name
  name: Headquarters
  domain: hq.internal           # suffix for the broker name; .internal is reserved for private use
  timezone: Europe/Berlin       # IANA; UI and daily rollups. The system clock stays UTC.
controller:
  hostname: ucc-hq
  hardware: pi4                 # pi4 | vm (test tier T4)
  rtc: ds3231                   # ds3231 | rv3028 | none
  ups_gpio: null                # e.g. 17 -> gpio-shutdown overlay
  ssd_reserve_percent: 0        # 10 for a consumer SSD without PLP
network:
  mode: trunk                   # trunk (802.1Q on eth0) | dual (USB NIC = bms0) | flat (one untagged LAN, lab only)
  parent: eth0
  mac: auto                     # auto: remember the first board's MAC and pin it after a board swap; null: never pin; or "dc:a6:32:.."
  it:  {vlan: 10, address: 192.0.2.20/24, gateway: 192.0.2.1, dns: [192.0.2.53]}   # or {vlan: 10, dhcp: true}
  bms: {vlan: 20, address: 10.20.0.2/24, nic_mac: null}                            # nic_mac: dual mode only
  sources:
    admin: [192.0.2.0/27]       # SSH
    ui: [192.0.2.0/24]          # HTTPS
    backup: [192.0.2.40/32]     # SFTP backup hosts (egress)
    bbmd: []                    # BACnet BBMD peers outside the BMS subnet
time:
  servers: [ntp1.corp.example]  # upstream NTP on the IT side; empty = isolated site
  nts: []                       # e.g. [ptbtime1.ptb.de] (needs tcp 4460 egress)
  serve: true
services:
  dhcp: {enabled: false, range: [10.20.0.100, 10.20.0.199], lease: 12h}
  syslog: {enabled: true}
  mqtt:
    topic_root: bacnet-uc
    extra_roots: []             # other APP_MQTT_TOPIC_ROOT values in use on this site
    broker_name: null           # default mqtt.<site.domain>
    expected_devices: null      # default: number of active entries in devices.yaml
    third_party_listener: false # 8884 TLS + password for non-mqtt_tls devices
  history:
    expected_points: 2000
    raw_days: 400
    log_days: 180
    event_days: 3650
    budget_percent: 60          # raw samples + device logs may use this share of /srv/uc
  hub:
    enabled: true
    users: [{user: ilija, roles: [admin]}]     # tokens are generated on the box
    llm: {provider: none}                      # none | zai (opens tcp 443 egress)
    manifest: null                             # path of the initial hub site.yaml; null = minimal manifest
    overrides: {}                              # merged into the rendered hub.yaml "drivers" section
  ui:
    names: [uc-ctl.corp.example]
    cert: site                  # site (issued by the site issuing CA) | provided (key + chain as secrets)
  updates: {online: true, auto_security: true}
backup:
  recipients: ["age1...integrator", "age1...owner"]          # public keys only
  targets:
    - {type: sftp, url: "sftp://uc-backup@192.0.2.40:22/uc/hq"}
    - {type: usb, label: UCBACKUP}
  history_repo: null            # e.g. "sftp://uc-backup@192.0.2.40:22/uc/hq-pg" (pgBackRest); null = no PG physical backup
  at: "02:15"
admins:
  - {name: ilija, keys: ["ssh-ed25519 AAAA... ilija@laptop"]}
ssh_user_ca: []                 # optional TrustedUserCAKeys lines (short-lived SSH certificates)
```

Semantic checks beyond the schema: addresses inside their subnets, VLAN ids
distinct, `bms.address` not in the DHCP range, recipients parse as age keys,
`hub.users` not empty when `hub.enabled`, `site.id` equal to the manifest's
`metadata.name` when a manifest is given.

**Derived values** (computed once, never written by hand; golden tests pin them):

| Value | Derivation |
|---|---|
| `bms_ip`, `bms_prefix`, `bms_net`, `bms_bcast` | from `network.bms.address` |
| hub `drivers.bacnet_ip.interface` | `<bms_ip>/<bms_prefix>` |
| hub `drivers.bacnet_uc.discover_broadcast` | `[<bms_bcast>:1337]`, never `255.255.255.255` (it leaves through the IT default route [V]) |
| hub `drivers.mqtt` | `127.0.0.1:1883`, user `uc-hub` |
| broker name | `services.mqtt.broker_name` or `mqtt.<site.domain>` |
| broker certificate SANs | broker name, hostname, `<bms_ip>` |
| DHCP options | 6 (DNS) = `bms_ip`, 7 (log server) = `bms_ip`, 42 (NTP) = `bms_ip`, no router option |
| chrony `allow` | `bms_net` |
| interface names | trunk: `it0`, `bms0` on `eth0`; dual: `eth0`→`it0`, USB NIC→`bms0`; flat: `eth0`→`bms0` |

### 5.2 Device registry

`/etc/uc-controller/devices.yaml` is maintained by `uc-ctl device ...`, not by
hand. It is part of the site bundle.

```yaml
devices:
  - {id: z04hkaps9lf6uu0938ljg, hwid: 0123456789abcdef01234567, name: r204-node,
     mac: "02:80:e1:12:34:56", ip: 10.20.0.10, status: active, serial: "4f1c...", issued: 2026-09-25}
```

It feeds the dnsmasq reservations, the ACL deny list (revoked devices) and the
health check's expected device count.

The Debian `dnsmasq` package starts a DNS server on all interfaces when it is
installed. The package and the image therefore disable `dnsmasq.service`, and a
drop-in adds `ConditionPathExists=/etc/dnsmasq.d/uc-bms.conf`; the `dhcp` role
renders that file and enables the unit only when `services.dhcp.enabled`. The
rendered file sets `interface=bms0`, `bind-dynamic`, `no-resolv`, `no-hosts`,
`dhcp-authoritative`, the options of §5.1, `host-record=<broker name>,<hostname>,<bms_ip>`
and one `dhcp-host=` line per active device with a MAC and IP.

### 5.3 `uc-ctl`: the single operator CLI

`/usr/sbin/uc-ctl` (Python, Debian packages only). Every command group is
implemented by exactly one work package (§23):

| Group | Commands | WP |
|---|---|---|
| Config | `render --out DIR [--only ROLES]`, `apply [--dry-run] [--first-boot] [--no-systemd] [--only ROLES]`, `confirm`, `rollback-config [--if-unconfirmed TXID]`, `check` | WP1 |
| Secrets | `secret set NAME` (stdin), `secret ensure`, `secret list` | WP1 |
| Hold | `bms up`, `bms down` | WP1 |
| Time | `time status`, `time set "YYYY-MM-DD HH:MM:SS"` | WP1 |
| PKI and devices | `pki import DIR`, `pki status`, `device add (--hwid HEX \| --id ID) [--name N] [--mac M] [--ip IP] [--pki DIR] [--out DIR]`, `device revoke ID`, `device list` | WP2 |
| History | `history status`, `history rebuild-daily`, `event KIND JSON` (post a controller event) | WP3 |
| Hub | `hub seed MANIFEST`, `token rotate USER`, `token show USER` (users and roles come from `services.hub.users`; `apply` generates missing tokens) | WP4 |
| State | `status [--json]`, `selftest [--hardware]`, `support-bundle [--out FILE]` | WP5 |
| Backup | `backup now`, `backup list`, `backup export PATH`, `bundle new --pki DIR [--devices FILE] --out FILE [--recipient KEY]` (without `--recipient` it also writes a one-time identity `FILE.key`), `restore FILE --identity (F \| -) [--no-hold]`, `restore-history [--from pgbackrest\|summary]` | WP5 |
| Updates | `update (--bundle FILE \| --online) [--with-kernel] [--force]`, `rollback` | WP5 |
| Platform | `firstboot`, `eeprom check`, `eeprom apply` | WP6 |

`device add --pki DIR` and `bundle new` also run on the integrator's laptop,
without a `controller.yaml` (office mode, §5.6).

### 5.4 The apply transaction

`uc-ctl apply` is the only way configuration reaches the system.

1. Load and validate `controller.yaml` and `devices.yaml` (schema + semantic checks).
2. Ensure secrets exist (`/etc/uc-controller/secrets/`, §13).
3. **Validation render:** every enabled role renders into
   `/var/lib/uc-controller/staging/<txid>/` with all absolute paths inside the
   files prefixed by the staging root.
4. Run each role's validators against the staged files:
   `nft -c -f`, `chronyd -p -f`, `dnsmasq --test -C`, `sshd -t -f`,
   `nginx -t -c`, `mosquitto --test-config -c`, `systemd-analyze verify`,
   `udevadm verify`, `postgres -C` (parse check), `uc-hub check -c` once C13
   exists (until then: load the config model from the hub venv). A validator
   whose program is missing is skipped with a warning.
5. **Install render:** render again with real paths. Diff against
   `/var/lib/uc-controller/installed.json` (path → sha256). No change: exit 0.
6. Snapshot every affected live file into `/var/lib/uc-controller/lkg/<txid>/`
   (with "absent" markers; keep the last 5). Write files with temp file,
   fsync, rename; set owner and mode. Remove files that a role no longer renders.
7. Run role `post_install` actions (for example regenerate the mosquitto
   password file), then reload or restart only the affected units, in role order.
8. Run the probes of the affected roles (§15). On failure: restore the LKG set,
   reload again, exit non-zero with a report.
9. If the network, firewall or SSH role changed and the session is remote
   (`SSH_CONNECTION` set), arm
   `systemd-run --on-active=180 --unit=uc-ctl-confirm-<txid> uc-ctl rollback-config --if-unconfirmed <txid>`.
   The operator runs `uc-ctl confirm` from a **new** SSH session.
10. Take a site bundle backup (§14.1).

`--no-systemd` skips all `systemctl` calls and probes and lists the skipped
actions in the JSON result. Containers use it (systemd cannot run as PID 1
under qemu-user [V]). `--dry-run` stops after step 5 and prints the diff with
secrets masked.

### 5.5 First boot (`uc-firstboot.service`)

Ordered after `local-fs.target`, before every service it configures. Each step
writes `/var/lib/uc-controller/firstboot/<step>.done`, so a power cut simply
repeats the step.

1. Remount FAT read-write. Read `uc-controller.yaml` and validate it. If it is
   invalid: write `uc-firstboot-report.txt` to FAT, keep only SSH (if admin keys
   parse) and stop in the "unconfigured" state with no BMS services. The
   technician fixes the file and power-cycles.
2. Grow p2 to 32 GiB, create and mount p3.
3. Create system users (sysusers.d), admin accounts (group `ssh-admins`,
   sudo with password, password set at first login with `chage -d 0`), SSH host
   keys, machine-id.
4. `uc-ctl secret ensure`.
5. `pg_createcluster 18 main -d /srv/uc/postgresql/18/main` (no default cluster
   exists; §4.1).
6. `uc-ctl apply --first-boot` (installs files and enables units, but does not
   start them: they start in normal boot order afterwards). Without PKI the
   broker stays down (`ConditionPathExists=` on its key) and health reports
   "PKI missing".
7. `uc-ctl eeprom apply` (no-op if already right).
8. Write `uc-firstboot-report.txt` to FAT: versions, MACs, board serial,
   backup SSH public key, broker certificate fingerprint (if PKI present),
   check results. Remount FAT read-only. Reboot once.

### 5.6 Install a new site

**Office (integrator laptop; `uc-pki` runs the same there):**
1. `uc-pki root --site hq --out ./hq-root` (offline root; key passphrase-protected).
2. `uc-pki issuing --root ./hq-root --site hq --out ./hq-pki`.
3. `uc-ctl device add --pki ./hq-pki --hwid <hex> --out ./fw` for known
   devices. Device certificates are needed to build the mqtt_tls images anyway.
4. `uc-ctl bundle new --pki ./hq-pki --devices ./devices.yaml --out hq-initial.age`. It
   encrypts to a fresh one-time age identity and writes it to `hq-initial.age.key`.
   The key travels separately from the bundle (password manager). `age -p`
   passphrases were rejected because age reads them only from a terminal.
5. Write `controller.yaml` and commit it.

**Site:**
1. Flash `uc-controller-<ver>-pi4.img.xz` to the SSD with Raspberry Pi Imager,
   "No customisation" (the image has no cloud-init).
2. Copy `uc-controller.yaml` to the `bootfs` volume.
3. Fit the RTC, plug the SSD into a blue port, connect Ethernet, power on.
   Wait about 5 minutes (includes one reboot).
4. Over SSH: `sudo uc-ctl restore hq-initial.age --identity -` (paste the key), then
   `sudo uc-ctl bms up`, `sudo uc-ctl selftest --hardware`, `uc-ctl status`.
   The selftest report is copied to FAT and `/srv/uc/reports/`.
5. Checklist: no SD card in the slot; label with site, hostname, BMS IP and release.

### 5.7 State inventory

Everything that persists is listed here. The same table drives backup (§14).

| Path | Domain | Content | Backup class |
|---|---|---|---|
| `/etc/uc-controller/controller.yaml` | root | site file | bundle (also in git) |
| `/etc/uc-controller/devices.yaml` | root | device registry | bundle |
| `/etc/uc-controller/secrets/` | root | generated and provided secrets | bundle (age-encrypted) |
| `/etc/uc-pki/` | root | issuing CA key and cert, root cert, index, serial, CRL | bundle |
| `/etc/ssh/ssh_host_*` | root | host keys | bundle |
| `/etc/uc-hub/site.yaml` | root | initial hub manifest (seed only) | bundle |
| `/var/lib/uc-hub/` | root | hub SQLite DB, lock, artifacts (≤2 GB) | bundle (`sqlite3 .backup`; artifacts ≤200 MB) |
| `/var/lib/mosquitto/` | root | persist-sqlite store (retained, historian queue) | none (devices republish) |
| `/var/lib/uc-controller/` | root | installed.json, lkg/, boot history, board.json, releases/, snapshots/ | board.json only |
| `/var/log/journal` | root | persistent journal | none |
| `/srv/uc/postgresql/18/main` | data | history database | pgBackRest (optional) + nightly history summary |
| `/srv/uc/backup/` | data | local bundles (last 30), history summaries | – |
| rendered files (`/etc/nftables.conf`, `/etc/mosquitto/...`, ...) | root | derived from the above | not backed up; re-rendered |

---

## 6. Network, zones and firewall

### 6.1 Stack and modes

**systemd-networkd** (in the image, disabled by default in Pi OS [V]).
NetworkManager and netplan are masked or purged. Reasons: static appliance
configuration, no D-Bus daemon or translation layer, deterministic ordering, and
the same behaviour in the qemu VM tier.

| Mode | Interfaces | Use |
|---|---|---|
| `trunk` (default) | `eth0` has no address; `it0` = VLAN `it.vlan`, `bms0` = VLAN `bms.vlan` | managed switch; port tagged-only with an unused native VLAN, BPDU guard |
| `dual` | `eth0` renamed `it0`; USB NIC matched by `bms.nic_mac` renamed `bms0` (`.link` file) | physically separate BMS network (BSI INF.14.A28), or no managed switch |
| `flat` | `eth0` renamed `bms0`; IT access only by source sets | lab and HIL only; `selftest` warns |

- `bms0` has a static address with `ConfigureWithoutCarrier=yes` and
  `IgnoreCarrierLoss=yes`, so the broker, chrony, the syslog socket and bacpypes3
  can always bind, even when the switch is down at boot.
- `network.mac` pins the `eth0` MAC with a `.link` file (§14.4).
- A 2020 report of GENET link flapping with 802.1Q is unresolved [U]. T5-9 runs
  the target switch for 72 h. If it flaps, disable EEE in the `.link` file, or use `dual`.

### 6.2 Zones

| Zone | Carries |
|---|---|
| IT (`it0`) | HTTPS UI, SSH, upstream NTP/NTS, DNS, backups, updates, optional LLM |
| BMS (`bms0`) | MQTT 8883 in, BACnet/IP 47808, SMP 1337 out, syslog 514 in, NTP 123 in, DHCP/DNS in (optional) |
| loopback | MQTT 1883 (hub, historian, health), hub HTTP 8080; PostgreSQL and the historian API are **Unix sockets only** |

The controller never forwards between zones. It is the IEC 62443 conduit
device between IT and BMS and is documented as such.

### 6.3 nftables (`/etc/nftables.conf`, rendered)

The ruleset tested by research in an isolated netns, with our names [V]:

```
table inet uc {
  set admin_v4  { type ipv4_addr; flags interval; elements = { <network.sources.admin> } }
  set ui_v4     { type ipv4_addr; flags interval; elements = { <network.sources.ui> } }
  set backup_v4 { type ipv4_addr; flags interval; elements = { <network.sources.backup> } }
  set bbmd_v4   { type ipv4_addr; flags interval; elements = { <network.sources.bbmd> } }

  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif lo accept
    fib saddr . iif oif missing drop                        # strict reverse path
    icmp type { echo-request, destination-unreachable, time-exceeded } limit rate 10/second accept
    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, destination-unreachable, packet-too-big } accept
    iifname "it0"  jump in_it
    iifname "bms0" jump in_bms                             # flat mode: bms0 jumps to both chains
    limit rate 5/minute log prefix "uc-drop-in " level info
  }
  chain in_it {
    tcp dport { 80, 443 } ip saddr @ui_v4 accept
    tcp dport 22 ip saddr @admin_v4 meter ssh4 { ip saddr limit rate 6/minute burst 6 packets } accept
    udp sport 67 udp dport 68 accept                       # only when it.dhcp
  }
  chain in_bms {                                          # hold mode: this chain is only "drop"
    tcp dport 8883 ip saddr <bms_net> meter mqtt4 { ip saddr limit rate 30/minute burst 60 packets } accept
    udp dport 123 ip saddr <bms_net> accept               # when time.serve
    udp dport 514 ip saddr <bms_net> accept               # when syslog.enabled
    udp dport 47808 ip saddr { <bms_net>, @bbmd_v4 } accept
    udp sport 1337 ip saddr <bms_net> accept              # replies to the SMP discovery broadcast (conntrack does not match them [V])
    udp dport 67 accept                                    # when dhcp.enabled
    udp dport 53 ip saddr <bms_net> accept                 # when dhcp.enabled
    tcp dport 53 ip saddr <bms_net> accept                 # when dhcp.enabled
  }
  chain output {
    type filter hook output priority 0; policy drop;
    ct state established,related accept
    oif lo accept
    icmp type { echo-request } accept
    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit } accept
    oifname "it0"  jump out_it
    oifname "bms0" jump out_bms
    limit rate 5/minute log prefix "uc-drop-out " level info
  }
  chain out_it {
    udp dport { 53, 123 } accept
    tcp dport 53 accept
    tcp dport 4460 accept                                  # NTS-KE, when time.nts
    tcp dport 443 accept                                   # when updates.online, llm, or an https backup target
    tcp dport 22 ip daddr @backup_v4 accept                # SFTP backup targets
    udp sport 68 udp dport 67 accept                       # when it.dhcp
  }
  chain out_bms {
    udp dport { 1337, 47808 } ip daddr { <bms_net>, 255.255.255.255 } accept
    udp dport 47808 ip daddr @bbmd_v4 accept
    udp sport 67 udp dport 68 accept                       # dnsmasq replies, when dhcp.enabled
  }
  chain forward { type filter hook forward priority 0; policy drop; }
}
```

- **Fail-safe ruleset:** the `uc-controller` package installs
  `/usr/share/uc-controller/nftables-failsafe.nft` and uses it as
  `/etc/nftables.conf` until the first successful apply: SSH and ICMP in on any
  interface except `bms0`, DHCP client, everything else dropped, no forwarding.
  A box that was never configured can neither expose the BMS nor lock out
  support.
- **Hold mode** (`/etc/uc-controller/hold` exists): `in_bms` and `out_bms` are
  only `drop`, and uc-hub does not start. Used by restore and drills, so a
  restored unit cannot contend with a still-running one.
- Docker is never used on the box: its DNAT would bypass this input policy.

---

## 7. Time and NTP

- **chrony 4.6.1** replaces systemd-timesyncd (installing it removes timesyncd [V]).
  **fake-hwclock 0.14** is installed as a second saved clock; it only moves the
  clock forward.
- **RTC**: `dtoverlay=i2c-rtc,<model>`. The kernel sets system time when rtc0
  registers, also for modular drivers (`RTC_HCTOSYS=y`) [R]. chrony's `rtcsync`
  keeps it disciplined.
- **Runtime policy** (`/usr/lib/uc-controller/uc-time-policy`, run as
  `ExecStartPre` of chrony): the RTC is **valid** when `/dev/rtc0` exists, reads
  without error (the DS3231 driver fails reads when the oscillator-stop flag is
  set), and its time is at least the image build time and at least the
  fake-hwclock saved time minus 1 h. The script writes
  `/run/uc-controller/chrony-local.conf`:
  - RTC valid: `local stratum 10`. An isolated site keeps serving correct time.
  - RTC invalid or missing: `local stratum 10 activate 0.5`. chrony serves only
    after one real sync since boot, so devices never get a stale clock.
  - Why not `activate` always: after a building-wide power cut the IT NTP is
    often down too, and a valid RTC must still be served. Why not `local`
    never: unsynchronised chrony answers stratum 0, and Zephyr 4.4.2's SNTP
    client rejects stratum 0 as kiss-o'-death [V].

```
# /etc/chrony/conf.d/10-uc.conf (rendered)
include /run/uc-controller/chrony-local.conf
server ntp1.corp.example iburst          # from time.servers
# server ptbtime1.ptb.de iburst nts      # from time.nts
allow 10.20.0.0/24                       # bms_net, when time.serve
manual                                   # `uc-ctl time set` -> chronyc settime (isolated sites)
ratelimit interval 1 burst 16
log tracking
```

The time role also renders `/etc/chrony/chrony.conf` itself: Debian's defaults
(`makestep 1 3`, `rtcsync`, `leapseclist`, `driftfile`, loopback-only command
port, which `chrony-wait` needs, `confdir /etc/chrony/conf.d`) without the
`pool` line.

- **`uc-time-ready.service`** (oneshot): succeeds at once if the RTC is valid or
  chrony is synchronised; otherwise waits at most 120 s, then continues and
  creates `/run/uc-controller/time-untrusted` (health alarm). uc-hub and the
  historian are ordered `After=` it. Supervision never waits more than 2 minutes.
- **Devices** (mqtt_tls after M7/FW-12): DHCP option 42 from dnsmasq or site
  DHCP, or `CONFIG_NET_CONFIG_SNTP_INIT_SERVER=<bms_ip>`. BACnet nodes get time
  from the hub later (C21).
- **Monitoring:** `chronyc -c tracking`; warn when not synchronised or on the
  local reference (refid `7F7F0101`) for 30 min; an SNTP query to `bms_ip` must
  not return stratum 0; `/dev/rtc0` present; |offset| > 1 s alarms.
- [U] whether `chronyc settime` under `manual` counts as "synchronised" for
  `activate`: tested in T3 (`controller/tests/core/`).

---

## 8. MQTT broker

### 8.1 Version

**mosquitto 2.1.2** from repo.mosquitto.org, pinned (§4.10). It has
`disable_client_cert_date_checks`, the persist-sqlite plugin (changes written as
they happen, flushed every 5 s, instead of periodic snapshots), per-listener
`plugin_load`/`plugin_use` (`acl_file`/`password_file` are deprecated in 2.1 and
removed in 3.0), `--test-config`, a systemd watchdog in its unit, and TLS cipher
logging [V]. The HIL rig uses 2.1.2. Cost: a third-party repo without Debian
security support; security advisories trigger an out-of-window update (§17).
Debian forky ships 2.1.2, so the repo goes away with the next platform generation.

### 8.2 `/etc/mosquitto/mosquitto.conf` (rendered; replaces the packaged file)

The packaged file sets `persistence true`, which 2.1.2 refuses together with a
persistence plugin, so the file is replaced, not extended [V].

```
log_dest syslog
log_facility 5
log_type error
log_type warning
log_type notice
log_type information
log_timestamp false
connection_messages true

persistence_location /var/lib/mosquitto/
global_plugin /usr/lib/<multiarch>/mosquitto_persist_sqlite.so   # path found at render time
plugin_opt_sync normal
plugin_opt_flush_period 5

persistent_client_expiration 14d
max_queued_messages 200000          # only uc-historian has a persistent session
max_queued_bytes 134217728
queue_qos0_messages true            # M6 logs are QoS 0
max_inflight_messages 20
max_packet_size 262144              # = the hub's default max_payload_bytes
max_keepalive 300
allow_zero_length_clientid false
global_max_clients 2000
set_tcp_nodelay true
sys_interval 10

plugin_load acl /usr/lib/<multiarch>/mosquitto_acl_file.so
plugin_opt_acl_file /etc/mosquitto/uc/acl
plugin_load passwd /usr/lib/<multiarch>/mosquitto_password_file.so
plugin_opt_password_file /etc/mosquitto/uc/passwd

# devices: mutual TLS; bound to all addresses, restricted by nftables (the VLAN address may not exist yet)
listener 8883
listener_allow_anonymous false
accept_protocol_versions 4,5
max_connections 1000
certfile /etc/mosquitto/uc/broker.fullchain.pem
keyfile /etc/mosquitto/uc/broker.key
cafile /etc/mosquitto/uc/client-ca.pem        # issuing CA(s) + root
crlfile /etc/mosquitto/uc/crl.pem
tls_version tlsv1.2                           # a minimum; TLS 1.3 is negotiated when offered
require_certificate true
use_identity_as_username true                 # username = certificate CN
use_username_as_clientid true                 # client id := CN; no id hijacking [V]
disable_client_cert_date_checks true
plugin_use acl

# local services
listener 1883 127.0.0.1
listener_allow_anonymous false
accept_protocol_versions 4,5
plugin_use passwd
plugin_use acl

# optional third-party MQTT devices (services.mqtt.third_party_listener)
#listener 8884
#listener_allow_anonymous false
#certfile /etc/mosquitto/uc/broker.fullchain.pem
#keyfile /etc/mosquitto/uc/broker.key
#tls_version tlsv1.2
#plugin_use passwd
#plugin_use acl
```

`use_username_as_clientid` is **not** set on 1883: the hub opens two sessions
(`uc-hub` and `uc-hub-discover-<hex>`) with the same username [R].

### 8.3 ACL (`/etc/mosquitto/uc/acl`, rendered, 0600 mosquitto)

```
user uc-hub
topic readwrite #                  # generic-json devices use arbitrary topics; '#' does not match $SYS
topic read $SYS/#
user uc-historian
topic read bacnet-uc/+/status      # repeated for every root in topic_root + extra_roots
topic read bacnet-uc/+/info
topic read bacnet-uc/+/event
topic read bacnet-uc/+/log
topic read $SYS/broker/#
user uc-health
topic readwrite uc-controller/health/#
topic read bacnet-uc/+/status
topic read $SYS/#
# revoked devices (from devices.yaml), before the patterns:
user z04hkaps9lf6uu0938ljg
topic deny #
pattern write bacnet-uc/%u/status  # repeated for every root
pattern write bacnet-uc/%u/info
pattern write bacnet-uc/%u/telemetry
pattern write bacnet-uc/%u/event
pattern write bacnet-uc/%u/log
pattern read bacnet-uc/%u/cmd
```

- Service account names contain `-`, so they can never equal a derived device
  id (alphanumeric only).
- The historian does **not** read `telemetry`. Point samples come only from the
  hub, so nothing is stored twice.
- The acl-file plugin never rejects a SUBSCRIBE; delivery is filtered by the
  read ACL [V]. A misprovisioned CN shows up as a device that never comes
  online, which the health check reports. The dynamic-security plugin is
  reconsidered when the hub manages devices at runtime.

### 8.4 Password file

`/etc/mosquitto/uc/passwd` holds PBKDF2 hashes for `uc-hub`, `uc-historian`,
`uc-health`. Plaintext passwords are secrets (`mqtt-<user>`). Hashes are salted,
so the file is regenerated with `mosquitto_passwd -b` only when the set of users
or passwords changes; `/etc/mosquitto/uc/passwd.src.sha256` records the input
hash. Owner mosquitto, mode 0600 (2.1 warns about other modes and will refuse
them later [R]).

### 8.5 Unit drop-in (`mosquitto.service.d/uc.conf`)

`Restart=always`, `RestartSec=2`, `LimitNOFILE=8192`,
`ExecStartPre=/usr/sbin/mosquitto --test-config -c /etc/mosquitto/mosquitto.conf`,
`ProtectSystem=strict`, `ReadWritePaths=/var/lib/mosquitto`, `ProtectHome=yes`,
`PrivateTmp=yes`, `NoNewPrivileges=yes`, `ConditionPathExists=/etc/mosquitto/uc/broker.key`,
resources from §4.8. mosquitto state is on root, so the broker survives the loss of `/srv/uc`.

---

## 9. Site PKI

### 9.1 Structure

ECDSA P-256 / SHA-256 only. This avoids the device-side RSA-PSS and TLS-1.2-RSA
caveats and the SHA-1 root problem, and P-256 is cheap on a Pi 4 without crypto
extensions [R].

| Certificate | Where the key lives | Profile |
|---|---|---|
| Site root | offline: integrator vault plus a second encrypted copy (owner) | CA, signs issuing CAs only |
| Issuing CA | controller `/etc/uc-pki/issuing.key` (0600 root); made at the office and carried in the initial bundle | CA, `pathlen:0`, signs broker, device, service certs and the CRL |
| Broker | `/etc/mosquitto/uc/broker.key` (0600 mosquitto) | serverAuth; SAN = broker name, hostname, `bms_ip` |
| Device | on the device (compiled in today; on-chip key + CSR after M5) | clientAuth; **CN = client id** (`z` + lower-case base32hex of the UID); no SAN |
| Service (`svc-*`) | on the controller, only if services ever use certificates | clientAuth |
| UI | IT PKI (preferred, `ui.cert: provided`) or the issuing CA | serverAuth; 825 days, renewed automatically |

Devices trust the **root**. A replacement controller or a new issuing CA needs
no reflash; the old issuing CA stays in `client-ca.pem` so its devices keep
working.

### 9.2 Fixed-epoch validity

All device-facing material uses **notBefore 2020-01-01T00:00:00Z and notAfter
2051-01-01T00:00:00Z**: root, issuing CA, broker, device and service
certificates. The CRL has `lastUpdate` 2020-01-01 and `nextUpdate`
2050-12-31, and is regenerated on every revocation, not on a timer.

Reasons:
- Devices check neither dates nor revocation today [R]. Rotation adds no
  security while nothing on the device side enforces it.
- An expired CRL locks out every device, even with
  `disable_client_cert_date_checks`; a CRL whose `lastUpdate` is later than the
  controller clock fails too [V]. Without a timer, nothing can forget to run.
- After M7, devices check broker certificate dates. A fixed 2020–2051 window
  contains every sane device clock. Unset clocks are handled by M10 (§21).
- Honest limit: a stolen broker or issuing key stays usable until devices get a
  new anchor (reflash, or M5). Rotation would not change that either, because
  devices do not check revocation.

The UI certificate is the exception: browsers enforce shorter lifetimes, so it
is 825 days and renewed by `uc-pki-maint.timer` (weekly) at < 300 days left. A
failed renewal affects browsers only.

### 9.3 `uc-pki` (`/usr/sbin/uc-pki`, bash + openssl 3.5)

Runs identically on the integrator laptop and on the controller.

```
uc-pki root --site SITE --out DIR                       # offline; root.key encrypted with a passphrase
uc-pki issuing --root DIR --site SITE --out PKI         # offline; issuing CA signed by the root
uc-pki broker --pki PKI --name NAME --dns HOST --ip IP  # writes broker.key, broker.crt, broker.fullchain.pem
uc-pki device --pki PKI (--id ID | --hwid HEX) --out DIR # ID.key (SEC1), ID.crt, ID.conf (Kconfig fragment)
uc-pki service --pki PKI NAME
uc-pki ui --pki PKI --name NAME [--name NAME...] [--ip IP]
uc-pki revoke --pki PKI CRT [REASON]
uc-pki crl --pki PKI
uc-pki client-id HEX                                    # derivation of device_id.c
uc-pki check --pki PKI [--json]                         # expiry report for health
```

- `openssl ca` with `rand_serial`, `copy_extensions=none`, `default_md=sha256`,
  `-startdate 20200101000000Z -enddate 20510101000000Z` for fixed-epoch profiles,
  and `-gencrl -crl_lastupdate 20200101000000Z -crl_nextupdate 20501231000000Z`.
- Profiles: `v3_issuing` (`basicConstraints=critical,CA:TRUE,pathlen:0`,
  `keyUsage=critical,keyCertSign,cRLSign`), `v3_broker`, `v3_client`, `v3_ui`
  (all `CA:FALSE`, `digitalSignature`, proper EKU).
- Client-id test vectors: hwid `0123456789abcdef01234567` →
  `z04hkaps9lf6uu0938ljg`; 16-byte hwid (MCX, SHA-256 fold) → `zl3teqqlrude1595idp00` [V].
- The Kconfig fragment holds `APP_MQTT_TLS_CLIENT_AUTH=y`, the CA, cert and key
  file paths, `APP_MQTT_BROKER_HOSTNAME=<bms_ip>` and
  `APP_MQTT_TLS_HOSTNAME=<broker name>`. With the IP as broker host and the name
  for SNI and verification, devices need no DNS.
- After a build: delete the key and the build directory (the build contains
  `zephyr/include/generated/app_creds/`).

### 9.4 Device lifecycle

- `uc-ctl device add --hwid HEX [--name N] [--mac M] [--ip IP]`: derive the id,
  issue the certificate, write the Kconfig fragment, add the entry to
  `devices.yaml`, re-render dnsmasq reservations.
- `uc-ctl device revoke ID`: `uc-pki revoke`, new CRL, `status: revoked` in
  `devices.yaml`, re-render the ACL (deny), reload mosquitto (SIGHUP). The ACL
  silences a live session at once; the CRL blocks the next connect. A connected
  client is not re-checked against the CRL [V].
- Later (M5): on-device key generation and a CSR over SMP; the controller signs
  with a narrow helper that the AI-facing hub can only reach through an
  approval (C-request C20).

---

## 10. History: trends, device logs, events

### 10.1 Decision

**PostgreSQL 18.6 (PGDG) + TimescaleDB 2.30.1 Community (TSL, packagecloud) +
timescaledb-toolkit 1.26.0**, in its own cluster on `/srv/uc`, written only by
**uc-historian**, read only through the historian's `/v1` API.

| Criterion | TimescaleDB TSL | Plain PostgreSQL 17 (Debian) |
|---|---|---|
| Storage | 2.3–5.7 B/sample compressed [V]; 4.0 B/row on a mixed 864 000-row set [V] | 60–92 B/sample, no compression [V] |
| Rollups, retention, compression | built-in continuous aggregates and policies; `rollup(time_weight)` exact across hours [V] | our own SQL and timers |
| Backups | ~12 GB at 5000 points × 400 days | ~265 GB; a weekly full over SFTP with AES in software on a Pi 4 takes hours |
| Repos | PGDG + packagecloud (pinned, edition guard) | Debian only |
| Licence | TSL: DDL must be technically or contractually prohibited for users; legal review before shipping a preinstalled image | none |

VictoriaMetrics (numeric only, no OSS downsampling), InfluxDB 3 Core (no
compactor, ~72 h query limit), QuestDB (JVM, Parquet automation Enterprise-only)
and SQLite (200–370× write amplification) each fail a hard requirement [V].

The TSL question is isolated: the hub sees only `/v1`. An Apache-only backend
(§10.18) can replace the storage layer without any hub change.

### 10.2 Packages, edition guard

- Pins in §4.10. The timescaledb package ships every library version back to
  2.23, so an apt upgrade never breaks the loaded version; `ALTER EXTENSION ...
  UPDATE` runs only in the post-commit step of an update (§17.3) [V].
- **Edition guard:** at start the historian checks
  `current_setting('timescaledb.license') = 'timescale'` and that the timescaledb
  and toolkit `extversion` are at least what its migrations need. If not, the
  writer does not start; the query API still serves.

### 10.3 Cluster and configuration

- Cluster: `pg_createcluster 18 main -d /srv/uc/postgresql/18/main`. Data
  checksums are on by default in PG 18 [V]; they catch a bridge that lies about
  flushes. Database `uc_history`.
- `/etc/postgresql/18/main/conf.d/uc.conf` (rendered):

```
listen_addresses = ''                      # Unix socket only
max_connections = 40
shared_preload_libraries = 'timescaledb'
timescaledb.license = 'timescale'
timescaledb.telemetry_level = off
timescaledb.max_background_workers = 8
max_worker_processes = 16
max_parallel_workers = 2
max_parallel_workers_per_gather = 1
shared_buffers = 512MB
effective_cache_size = 2GB
work_mem = 16MB
maintenance_work_mem = 256MB
jit = off
wal_compression = lz4                      # -28 % WAL [V]
wal_buffers = 16MB
checkpoint_timeout = 30min
max_wal_size = 2GB
min_wal_size = 512MB
full_page_writes = on
fsync = on
synchronous_commit = on                    # the hub-readings writer session sets off (§10.14)
random_page_cost = 1.1
effective_io_concurrency = 64
default_toast_compression = lz4
autovacuum_max_workers = 3
track_io_timing = on
log_min_duration_statement = 1000
archive_mode = on                          # always on; archive_command decides (restart-free enable)
archive_command = '/usr/lib/uc-controller/pg-archive %p'   # pgbackrest archive-push when history_repo is set, else exit 0
archive_timeout = 60
```

- `pg_hba.conf`: `local all postgres peer` and
  `local uc_history uc_historian,uc_hist_ro peer map=uc`. No TCP.
- `pg_ident.conf` map `uc`: OS `uc-historian` → `uc_historian` and `uc_hist_ro`;
  each admin → `uc_hist_ro`.
- Drop-in `postgresql@18-main.service.d/uc.conf`: `RequiresMountsFor=/srv/uc`,
  `ConditionPathIsMountPoint=/srv/uc`.

### 10.4 Schema `hist` (migrations `0000`–`0004`)

The datatypes follow the hub's `Datatype` contract (`core/types.py`) [V]:
`real` float; `int` whole number (multi-state state numbers 1..n, counters);
`enum` the two-state BACnet binary present value 0/1; `bool`; `string`.

```sql
-- 0000 (as postgres): database, roles
CREATE ROLE uc_historian LOGIN; CREATE ROLE uc_hist_ro LOGIN;
ALTER ROLE uc_hist_ro SET default_transaction_read_only = on;
ALTER ROLE uc_hist_ro SET statement_timeout = '5s';
-- CREATE DATABASE uc_history OWNER uc_historian; CREATE EXTENSION timescaledb; CREATE EXTENSION timescaledb_toolkit;

-- 0001 (as uc_historian): tables
CREATE SCHEMA hist;
CREATE TABLE hist.schema_migrations (version int PRIMARY KEY, name text, checksum text, applied_at timestamptz DEFAULT now());
CREATE TABLE hist.device (
  id serial PRIMARY KEY, name text NOT NULL UNIQUE,
  key text UNIQUE,                    -- 'hwid:<hex>' | 'mqtt:<client id>' | 'bacnet:<instance>' | 'topic:<t>' | 'name:<n>'
  protocol text NOT NULL, space text, mac text,
  first_seen timestamptz NOT NULL DEFAULT now(), retired_at timestamptz);
CREATE TABLE hist.point (
  id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ref text NOT NULL UNIQUE,           -- hub PointRef 'hq/r204-ctl/analog-input:1'
  device_id int NOT NULL REFERENCES hist.device, obj text NOT NULL,
  name text, kind text, datatype text NOT NULL CHECK (datatype IN ('real','int','enum','bool','string')),
  units text, states text[], space text, tags text[] NOT NULL DEFAULT '{}',
  trended boolean NOT NULL DEFAULT true,
  deadband real, min_interval_s real, heartbeat_s int,   -- NULL = defaults (§10.9)
  created_at timestamptz NOT NULL DEFAULT now(), retired_at timestamptz,
  UNIQUE (device_id, obj));                               -- identity survives renames
CREATE TABLE hist.point_alias (ref text PRIMARY KEY, point_id int NOT NULL REFERENCES hist.point, until timestamptz NOT NULL);

CREATE TABLE hist.sample (
  ts timestamptz NOT NULL, point_id int NOT NULL,         -- no FK (per-row cost); the registry validates
  value double precision,                                 -- real/int; enum/bool as 0/1; NULL for string
  text text,                                              -- datatype string only
  quality smallint NOT NULL DEFAULT 0,                    -- 0 good 1 stale 2 fault 3 offline
  priority smallint,                                      -- active BACnet priority of commandables
  src smallint NOT NULL DEFAULT 0                         -- 0 hub 1 hub+device-ts 2 backfill 3 keep 4 import
) WITH (tsdb.hypertable, tsdb.partition_column='ts', tsdb.chunk_interval='{{ sample_chunk }}',
        tsdb.segmentby='point_id', tsdb.orderby='ts DESC');
CREATE UNIQUE INDEX sample_point_ts ON hist.sample (point_id, ts DESC);   -- idempotent retries, also on compressed chunks [V]

CREATE TABLE hist.device_log (
  ts timestamptz NOT NULL, source text NOT NULL,          -- client id or syslog host/IP
  device_id int, origin smallint NOT NULL,                -- 0 mqtt-live 1 mqtt-fetch 2 syslog
  level smallint NOT NULL,                                -- 0 dbg 1 inf 2 wrn 3 err
  src text, msg text NOT NULL, boot_no int, dev_uptime_ms bigint, dev_ts_valid boolean NOT NULL DEFAULT false,
  line_key bigint NOT NULL                                -- hash(boot, t, lvl, src, msg)
) WITH (tsdb.hypertable, tsdb.partition_column='ts', tsdb.chunk_interval='7 days',
        tsdb.segmentby='source', tsdb.orderby='ts DESC');
CREATE UNIQUE INDEX ON hist.device_log (source, line_key, ts);

CREATE TABLE hist.event (
  ts timestamptz NOT NULL, kind text NOT NULL, device_id int, point_id int,
  actor text, severity smallint NOT NULL DEFAULT 1, origin smallint NOT NULL, data jsonb NOT NULL DEFAULT '{}'
) WITH (tsdb.hypertable, tsdb.partition_column='ts', tsdb.chunk_interval='30 days',
        tsdb.segmentby='kind', tsdb.orderby='ts DESC');
CREATE INDEX ON hist.event (device_id, ts DESC);
CREATE UNIQUE INDEX ON hist.event ((data->>'_id'), ts) WHERE data ? '_id';   -- client-supplied event ids

CREATE TABLE hist.device_state (device_id int PRIMARY KEY, online boolean, since timestamptz, info_hash text);
CREATE TABLE hist.device_boot (source text, boot_no int, device_id int, boot_id text, epoch timestamptz NOT NULL,
  detected_at timestamptz NOT NULL, PRIMARY KEY (source, boot_no));
CREATE TABLE hist.ingest_session (session text PRIMARY KEY, kind text, started_at timestamptz, last_seq bigint, last_at timestamptz);
CREATE TABLE hist.heartbeat (id int PRIMARY KEY CHECK (id = 1), at timestamptz NOT NULL);
```

Migration gotcha [V]: `WITH (tsdb.hypertable ...)` creates a columnstore policy
automatically (compress after one chunk interval). `0003` removes it with
`remove_columnstore_policy` before adding its own.

### 10.5 Rollups, compression, retention, space budget

```sql
-- 0002
CREATE MATERIALIZED VIEW hist.sample_1h WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 hour', ts) AS bucket, point_id,
       min(value) AS min, max(value) AS max, first(value, ts) AS first, last(value, ts) AS last,
       time_weight('LOCF', ts, value) AS tw, count(*) AS n, count(*) FILTER (WHERE quality <> 0) AS n_bad
FROM hist.sample WHERE value IS NOT NULL GROUP BY 1, 2 WITH NO DATA;
SELECT add_continuous_aggregate_policy('hist.sample_1h', start_offset => '3 days', end_offset => '1 hour',
                                       schedule_interval => '30 minutes');
CREATE MATERIALIZED VIEW hist.sample_1d WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', bucket, '{{ timezone }}') AS day, point_id, min(min) AS min, max(max) AS max,
       rollup(tw) AS tw, sum(n) AS n, sum(n_bad) AS n_bad
FROM hist.sample_1h GROUP BY 1, 2 WITH NO DATA;
SELECT add_continuous_aggregate_policy('hist.sample_1d', start_offset => '4 days', end_offset => '1 day',
                                       schedule_interval => '6 hours');
```

Verified on 2.30.1 / toolkit 1.26.0 [V]: the real-time hourly view shows rows
inserted after the last refresh; `average(rollup(tw))` over 24 hourly rows
equals `time_weight` over the raw day exactly; a daily rollup with
`'Europe/Berlin'` produces local days (the DST day 2026-10-25 has 25 hours).
Sites in half-hour-offset timezones get UTC days. Changing `site.timezone`
needs `uc-historian rebuild-daily`.

Policies are not migrations: `uc-historian apply-policies` (idempotent, run by
the historian at start and by apply) sets them from `historian.yaml`:

| Object | Chunk | Compress after | Retention |
|---|---|---|---|
| `sample` | 1 day while `expected_points × 1440` ≤ 5 M rows/day; 12 h up to 10 M; else 6 h | chunk interval + 6 h | `raw_days` (400) |
| `sample_1h` | automatic | 30 days | none |
| `sample_1d` | automatic | 365 days | none |
| `device_log` | 7 days | 7 days | `log_days` (180) |
| `event` | 30 days | 90 days | `event_days` (3650) |

**Space budget** (historian ops loop, hourly): if
`hypertable_size(sample) + hypertable_size(device_log)` exceeds
`budget_percent` of `/srv/uc`, drop the oldest `sample` chunks, then the oldest
`device_log` chunks, until under budget, and write a
`history.retention_space` event (warning). Rollups and events are never
dropped automatically.

**Disk guard:** at ≥ 95 % used on `/srv/uc` the historian answers 503 to
`/v1/readings`, drops `dbg`/`inf` device logs, and writes `history.paused`.
It resumes below 90 %.

### 10.6 uc-historian process

```
 BMS VLAN                                   uc-controller
 mqtt_tls nodes ─8883 mTLS─► mosquitto 2.1.2 ◄─1883 lo─ uc-hub (SMP, BACnet/IP, MQTT drivers; SQLite on /var/lib/uc-hub)
   status/info/event/log        │ persistent session "uc-historian"       │ C2 POST /v1/readings   C3 PUT /v1/points
   (+ telemetry → hub only)     │ QoS 1, ack after COMMIT                  │ C8 POST /v1/events     C5–C7 GET /v1/series|stats|logs|events
                                ▼                                          ▼   (Unix socket /run/uc-historian/api.sock)
 BACnet-uc nodes ─syslog UDP 514─► uc-historian (Python 3.13 asyncio; Debian python3-asyncpg 0.30, -aiohttp 3.11, -paho-mqtt 2.1)
                                     ├─ writer pool (role uc_historian)
                                     ├─ query pool  (role uc_hist_ro: read-only, 5 s timeout)
                                     └─ ops loop: heartbeat, gap and clock-step events, policies, space budget, disk guard, metrics
                                                   ▼
                                     PostgreSQL 18.6 + TimescaleDB 2.30.1 (+ toolkit), Unix socket, peer auth
```

- One process, one asyncio loop. paho runs its network thread and hands messages
  to the loop with `call_soon_threadsafe`. paho is used because aiomqtt has no
  manual acknowledgement and is not packaged in Debian [V].
- All runtime dependencies come from Debian trixie [V]. No pip, no venv; security
  fixes arrive through apt.
- Config `/etc/uc-controller/historian.yaml` (rendered): database DSN, MQTT
  (host, port, user, credential name, roots), syslog bind (fallback when not
  socket-activated), policy defaults, retention, timezone, budgets, API socket.
- CLI: `uc-historian serve | migrate [--post-commit] | apply-policies | check | rebuild-daily | export-summary DIR`.
- Unit `uc-historian.service`: `User=uc-historian`,
  `ExecStartPre=+/usr/lib/uc-controller/historian-migrate` (runs migrations as
  `postgres` via peer, then `apply-policies`), `Sockets=uc-historian-syslog.socket`,
  `After=postgresql@18-main.service mosquitto.service uc-time-ready.service`,
  `Wants=postgresql@18-main.service`, `RequiresMountsFor=/srv/uc`,
  `ConditionPathIsMountPoint=/srv/uc`,
  `Type=notify`, `WatchdogSec=60`, `Restart=always`, `RuntimeDirectory=uc-historian`,
  `LoadCredential=mqtt-uc-historian:/etc/uc-controller/secrets/mqtt-uc-historian`,
  resources from §4.8, and the hardening set of §11.3.
- API socket `/run/uc-historian/api.sock`, mode 0660, group `uc-hist-api`
  (members `uc-hub`, `uc-health`, admins). Write endpoints additionally require
  `SO_PEERCRED` uid ∈ {uc-hub, root}.
- Syslog: `uc-historian-syslog.socket` (rendered) with
  `ListenDatagram=<bms_ip>:514` and `FreeBind=yes`. The historian receives the
  file descriptor and needs no capability. Without socket activation (tests) it
  binds the configured address itself.

### 10.7 Ingest: hub readings (`POST /v1/readings`, hub request C2)

```json
{"source": {"kind": "uc-hub", "session": "6f1c2a9e", "seq": 1842, "dropped": 0},
 "readings": [
  {"id": "hq/r204-ctl/analog-input:1",  "ts": 1790323201.482, "v": 21.5, "q": "good"},
  {"id": "hq/r204-ctl/binary-output:2", "ts": 1790323201.482, "v": 1, "q": "good", "p": 12},
  {"id": "hq/r204-node/status",         "ts": 1790323199.004, "v": "online"},
  {"id": "hq/r204-node/telemetry.uptime_s", "ts": 1790323198.250, "v": 5230, "dts": true},
  {"id": "hq/r204-ctl/analog-output:1", "ts": 1790323202.000, "v": 55.0, "keep": true}
 ]}
```

| Field | Meaning |
|---|---|
| `id` | hub PointRef or an alias |
| `ts` | Unix seconds, float (`Reading.ts`) |
| `v` | number, bool, string or null. BACnet REAL arrives as widened float32; JSON's shortest repr round-trips exactly, and it compresses at ~2.3 B/row [V] |
| `q` | `good` (default), `stale`, `fault`, `offline` |
| `p` | active priority 1..16 |
| `dts` | `ts` came from the device clock (M7) |
| `keep` | bypass the storage policy (tests, commissioning evidence) |

- Limits: ≤ 5000 readings or 1 MiB per request.
- Responses: `200 {"accepted":n,"stored":k,"filtered":f,"unknown":u,"rejected":{"future":x,"too_old":y}}`;
  `400` malformed (hub logs and drops the batch); `503` + `Retry-After` when the
  database is down or the disk guard is active (hub keeps the batch and retries).
- Idempotent: one statement per batch,
  `INSERT ... SELECT * FROM unnest($1::timestamptz[], ...) ON CONFLICT DO NOTHING`,
  verified for duplicates and late rows in compressed chunks [V]. Filter state
  advances only after the commit, so a retried batch is filtered the same way.
- `seq` increases per hub session. A gap, or `dropped > 0`, writes a
  `history.gap` event. A new `session` writes `hub.start`.
- Rejected: `ts` more than 300 s in the future; `ts` older than `raw_days`.

### 10.8 Point registry (`PUT /v1/points`, hub request C3)

```json
{"site": "hq", "revision": 12, "complete": true,
 "devices": [{"name": "r204-ctl", "protocol": "bacnet-uc", "key": "hwid:0123456789abcdef01234567",
              "instance": 2041, "space": "r204", "mac": "02:80:e1:12:34:56"},
             {"name": "r204-node", "protocol": "mqtt", "key": "mqtt:z04hkaps9lf6uu0938ljg"}],
 "points": [{"id": "hq/r204-ctl/analog-input:1", "name": "R204 Temp", "kind": "input", "datatype": "real",
             "units": "degrees-celsius", "tags": ["Zone_Air_Temperature_Sensor"], "space": "r204",
             "trended": true, "history": {"deadband": 0.1, "min_interval_s": 60, "heartbeat_s": 900}},
            {"id": "hq/r204-ctl/multi-state-value:3", "datatype": "int", "states": ["Off", "Low", "High"], "trended": true}]}
```

- Sent by the hub at start, after every live manifest revision, and when a device
  description changes. Readings for unknown ids are counted, not stored.
- `complete: true`: points missing from the snapshot get `retired_at`; their
  history stays queryable.
- Device identity uses `key` in this order: hwid, MQTT client id, BACnet
  instance, generic-json topic, name. When a key reappears under a new name, the
  historian rewrites `ref` and keeps the old ref in `hist.point_alias`, so series
  continue and old ids still resolve.
- Response: `{"points":n,"added":a,"retired":r,"renamed":m}`.

### 10.9 Storage policy (in the historian; per-point overrides from C3/C4)

A reading is **stored** when any of these holds:
1. `keep` is set;
2. it is the first sample of the point, or its quality differs from the last stored sample;
3. `enum`, `bool`, `int` (without deadband), `string`: the value differs from the last stored value;
4. `real` (and `int` with `deadband > 0`): |v − last stored| ≥ `deadband` **and** at least `min_interval_s` since the last stored sample;
5. `heartbeat_s` has elapsed since the last stored sample.

A reading suppressed only by `min_interval_s` becomes **pending**; the newest
pending value is flushed when the interval expires, so the end of a step is
never lost. On start the historian loads the last stored sample per point from
the newest chunk.

| Default | real | int | enum / bool | string |
|---|---|---|---|---|
| `min_interval_s` | 60 | 0 | 0 | 0 |
| `deadband` | by unit, else 0 | 0 | – | – |
| `heartbeat_s` | 900 | 900 | 900 | 3600 |

Default deadbands by BACnet unit (`units.py`): degrees-celsius 0.05; percent and
percent-relative-humidity 0.2; parts-per-million 5; pascals 1; kilopascals 0.01;
volts 0.05; amperes 0.01; kilowatts 0.05; kilowatt-hours 0.1;
cubic-meters-per-hour 0.1; lux 5; hertz 0.05.

The 1 row/min cap for analog points bounds the worst case to
`expected_points × 1440` rows/day. Known limit: a spike shorter than
`min_interval_s` that returns within the deadband is not stored; such points get
a lower `min_interval_s` in the manifest.

### 10.10 MQTT archive (needs no hub change)

- paho 2.1 client `uc-historian`, `clean_session=False`, `manual_ack=True`,
  QoS 1 subscriptions to `<root>/+/{status,info,event,log}` for each root.
- Messages collect into a batch (≤500 or 1 s). The transaction commits with
  `synchronous_commit=on`; **only then** `client.ack(mid, qos)`. Verified with
  paho 2.1.0: unacknowledged messages are redelivered with DUP=1 after an
  unclean reconnect [V] (on mosquitto 2.0.21; 2.1.2 is re-checked in T3).
- While PostgreSQL is down the historian stops acknowledging and disconnects
  cleanly; the broker queues up to 200 000 messages / 128 MiB.
- `<id>` maps to a device through key `mqtt:<id>`. Unknown ids are stored under
  `source=<id>`, which shows talking-but-unregistered devices during commissioning.

| Topic | Stored as |
|---|---|
| `status` | `device_state` snapshot; `device.online` / `device.offline` event only when it differs (reconnects replay retained messages). A live `offline` is the LWT; its time is late by up to 1.5 × keepalive (90 s); `data.lwt = true`. |
| `info` | `device.info` event when the payload hash changes (firmware history) |
| `event` | `device.reply` event; payload ≤ 4 KiB as jsonb (text if not JSON). A `logs` command reply is parsed into `device_log` rows (origin 1). |
| `log` (M6) | `device_log` row, origin 0: `t`, `lvl`, `src`, `msg`; plus `ts` (M7) and `boot`/`seq` (M9) when present |

### 10.11 Syslog from BACnet-uc nodes

- RFC 5424 with RFC 3164 fallback. Level from PRI severity: 0–3 err, 4 wrn, 5–6 inf, 7 dbg.
- Device mapping: HOSTNAME `bacnet-uc-<mac>` → registry `mac`; else source IP via
  `devices.yaml`/registry; else `source=<ip>`.
- A TIMESTAMP ≥ 2020 is used as is; anything earlier is treated as uptime and
  mapped like M6 logs (§10.13).
- Token bucket per source: 50 lines/s, burst 500. `msg` capped at 1 KiB, control
  characters stripped, parameterised SQL only. Device text is stored, never interpreted.
- If a SIEM is required, rsyslog takes 514, forwards to the SIEM over TLS and to
  the historian on `127.0.0.1:5514` (optional role, not v1).

### 10.12 Events

Closed, namespaced vocabulary; `severity` 0 debug … 3 critical:

| Namespace | Kinds |
|---|---|
| `device.` | online, offline, boot, info, reply |
| `point.` | write, relinquish, force, release |
| `lease.` | expire |
| `bridge.` | start, stop, relinquish |
| `plan.` | apply, stage, rollback |
| `approval.` | decided |
| `alarm.` | raise, clear |
| `test.` | run |
| `hub.` | start, stop |
| `history.` | gap, paused, resumed, retention_space |
| `controller.` | start, stop, clock_step, backup, disk, cert, update, board_replaced |

Sources: the hub (C8, `actor` = user or `run:<id>`), the MQTT archive, the
historian ops loop, and `uc-ctl event KIND JSON` for scripts (backup, PKI, updates). `POST /v1/events`
uses the same session/seq envelope as readings; an optional client `id` (UUID,
stored as `data._id`) makes it idempotent.

### 10.13 Timestamps, boots, gaps

- Samples: `ts` = `Reading.ts` (hub receive clock, or device clock with `dts`).
- Device logs with M7 `ts`: exact. Without: `ts = boot_epoch + t`, where
  `boot_epoch` is fixed at the first line seen in that boot (`receive_time − t`).
  A new boot is detected when `t` goes backwards, or on a new M9 boot id.
  Because `ts` is deterministic per line, the live copy and a later `logs` fetch
  collapse into one row via `UNIQUE (source, line_key, ts)`, also in compressed
  chunks [V]. M9 makes this exact.
- `hist.heartbeat` is updated every 30 s. At start the historian writes
  `history.gap {from: last heartbeat, reason: "historian down"}`.
- Clock steps: wall versus monotonic clock checked every second; a step above
  0.5 s writes `controller.clock_step` with the offset.
- Per-point gaps are not stored as rows; the query layer computes validity
  (§10.15). `time_weight` bridges NULL rows with LOCF [V], so marker rows would not help.

### 10.14 Durability and backpressure

| Path | Commit mode | Worst loss on power cut |
|---|---|---|
| Hub readings (`/v1/readings`) | session `synchronous_commit=off` | ~3 × `wal_writer_delay` ≈ 0.6 s of acknowledged rows (same order as the hub's RAM buffer) |
| MQTT archive | `synchronous_commit=on`, ack after commit | none acknowledged; unacknowledged messages are redelivered |
| Events via `/v1/events` | `synchronous_commit=on` | none acknowledged |
| Syslog | `synchronous_commit=off` | UDP is lossy anyway |

Hub side (C2): bounded queue of 200 000 readings. On overflow it **coalesces**
to the newest reading per point and counts the dropped readings in `dropped`.
Memory stays bounded by the number of points, and the historian records the gap.

### 10.15 Query API

All on `/run/uc-historian/api.sock`, group `uc-hist-api`, limits enforced by
the server. Times are Unix seconds (float) or RFC 3339 on input. Ids are hub
refs or aliases.

| Endpoint | Purpose | Limits |
|---|---|---|
| `GET /v1/series?point=&start=&end=&buckets=200&agg=avg&quality=good` | one series | ≤1000 buckets, ≤10 years |
| `POST /v1/series {points[], start, end, buckets, agg}` | aligned multi-series | ≤20 points |
| `GET /v1/stats?point=&start=&end=` (POST for ≤20 points) | min, max, time-weighted avg, first, last, count, good_ratio, coverage; binary: duty cycle, on-hours, changes; multi-state: time per state (raw tier) | – |
| `GET /v1/logs?device=&source=&since=&until=&min_level=wrn&q=&limit=200` | newest first; `q` = substring | ≤500 lines |
| `GET /v1/events?kind=device.*&device=&point=&since=&until=&limit=200` | timeline | ≤500 |
| `GET /v1/catalog?device=&q=&trended=` | points with first/last ts, rows in last 7 days, effective policy, aliases | ≤2000 |
| `GET /v1/health` | db up, edition, extension versions, ingest lag, hub last batch age, MQTT connected, failed jobs, archive status, disk | – |

**Tier routing** (`query.py`), with `R = end − start`, `w = R / buckets`:
1. **samples:** if the stored rows in range are ≤ `buckets`, return raw samples `(t, v, q)`.
2. **raw:** if `w < 1 h` and `start` is inside raw retention: fetch rows from
   `start − 2 × heartbeat` to `end` (≤200 000 rows, else go to tier 3) and bucket
   in Python (LOCF time-weighted average, min, max, first, last, count). A sample
   is valid until the next sample or `ts + 2 × heartbeat_s`, whichever is first;
   a non-good sample ends the validity of the previous value. `cov[i]` is the
   valid fraction of bucket i; buckets with no valid time are `null`.
3. **1h:** if `1 h ≤ w < 1 d`: `sample_1h`, `w` rounded to whole hours:
   `average(rollup(tw))`, `min(min)`, `max(max)`, `last(last, bucket)`, `sum(n)`.
4. **1d:** if `w ≥ 1 d`: `sample_1d` (site-local days), `w` rounded to whole days.

`gaps[]` lists overlapping controller/hub/historian gap events.

```json
{"point": "hq/r204-ctl/analog-input:1", "datatype": "real", "units": "degrees-celsius",
 "tier": "1h", "bucket_s": 3600, "start": 1789718400, "end": 1790323200,
 "t": [1789718400, 1789722000], "v": [21.4, 21.6], "min": [21.1, 21.3], "max": [21.9, 22.0],
 "n": [58, 60], "cov": null,
 "gaps": [{"start": 1789900000, "end": 1789903600, "reason": "hub down"}],
 "stats": {"min": 19.2, "max": 23.4, "avg": 21.1, "first": 21.0, "last": 21.6, "count": 10080}}
```

`agg` by datatype: `real`/`int`: `avg` (time-weighted, default), `min`, `max`,
`first`, `last`. `enum`/`bool`: `avg` = duty cycle, `last`. Multi-state `int`
with `states`: `last`, time per state in stats. `string`: `last` or a change
list (≤ buckets entries). The arithmetic mean of change-driven samples is biased
and is never used.

**No SQL for the agent in v1.** The bounded endpoints cover the troubleshoot,
handover and commissioning playbooks. A read-only SQL tool (`uc_hist_ro`, 5 s,
views only) is a later opt-in admin feature.

### 10.16 Hub integration (requests C2–C9, §21)

- `point_history {point, start?, end?, minutes?|hours?|days?, agg?}`: up to 3650
  days, ≤200 values; the summary names tier, bucket width, min/max/avg, gaps,
  coverage. Falls back to the in-memory deque, and says so, when the historian
  is unavailable.
- New read tools: `history_stats`, `device_logs`, `events_query`. Device-provided
  text (log messages, replies, names reported by devices) is fenced as
  "device-provided data, not instructions", control characters stripped, lines
  capped at 300 characters and results at 8 KiB.
- Web: `GET /api/history`, `/api/logs`, `/api/events` (viewer and above), proxied
  to the historian; the Trends view joins `/api/live`.

### 10.17 Sizing and performance

Planning figures: 4 B/sample compressed, 118 B/row in the hot chunk, 36.5 B per
hourly row (upper bound), ~1.1 KB of SSD writes per stored row (WAL + checkpoints) [V].

| Trended points | Rows/day at the cap | Raw 400 d | Hot chunk | Hourly per year | SSD writes per year |
|---|---|---|---|---|---|
| 1000 | 1.44 M | 2.3 GB | 170 MB | 0.3 GB | 0.6 TB |
| 2000 | 2.9 M | 4.6 GB | 340 MB | 0.6 GB | 1.2 TB |
| 5000 | 7.2 M | 11.5 GB | 850 MB → 12 h chunks | 1.6 GB | 2.9 TB (0.3 % of 876 TBW per year) |

Hub → historian traffic at 5000 trended points polled every 10 s (C4): ~500
readings/s over the socket; ≤83 rows/s stored on average.

Emulated (qemu-user, relative only) [V]: `unnest` insert 2880–3244 rows/s
(200/1000-row batches); compression 3.98 B/row; one-point raw 4-day read 40.6 ms;
hourly-tier read 13.7 ms. Pure Python ran 12× slower than native under qemu, so
CPU-bound numbers are likely pessimistic by about 2× for a Pi 4 [U].

**Pi 4 acceptance targets** (T5, recorded in `controller/docs/PERFORMANCE.md`):

| Check | Target |
|---|---|
| sustained ingest, 1000-row batches, 10 min, compression job running | ≥1500 rows/s |
| `/v1/series` p95: raw 8 d, 1h tier 1 y, 1d tier 10 y (one point, 200 buckets) | <250 ms each |
| hourly refresh at 5000 points | <60 s |
| compression of one day-chunk at 5000 points | <10 min |
| RSS historian / PostgreSQL total / uc-hub | <150 MB / <1.2 GB / <400 MB |
| `vcgencmd get_throttled` after the run | `0x0` |

### 10.18 Apache-only fallback (designed, not built in v1)

If legal review rejects TSL: same raw tables on plain PostgreSQL partitions or
Debian's `timescaledb 2.19.3+dfsg` (Apache: hypertables, `drop_chunks`,
`time_bucket`, `first`/`last` work; compression, caggs and policies do not [V]).
`migrations/apache/0002` creates plain `sample_1h`/`sample_1d` tables filled by
historian jobs (`INSERT ... SELECT` with LOCF window functions; the toolkit is
TSL too). Raw retention drops to about 90 days (92–118 B/row). The `/v1` API is
unchanged.

---

## 11. uc-hub hosting

### 11.1 Package

`uc-hub_<hubver>+uc<N>_arm64.deb`, built by `controller/hub-pkg/build.sh`:

- Source: a pinned commit of `claude/ai-harness-building-control-05nrgm`
  (`controller/hub-pkg/hub.ref`: commit, web/dist and uc-link.wasm digests).
- venv at `/opt/uc-hub/venv` on the system `/usr/bin/python3` (3.13.5), built in
  an arm64 trixie container with `pip install --require-hashes --no-deps -r
  requirements.lock`. All wheels are pure Python or cp313 aarch64 [V]. The hub
  installs and runs on arm64 trixie [V].
- `web/dist` → `/usr/share/uc-hub/web` (architecture-independent; built natively
  in a Node 20.19 container with corepack pnpm).
- The stock `uc-link.wasm` (from the BACnet branch's `wasm/examples/uc-link` with
  uc-cc; never the demo stand-in) → `/usr/share/uc-hub/wasm/` when available.
- `dpkg-deb --root-owner-group`. `Depends: python3 (>= 3.13), python3 (<< 3.14)`.
- Python dependency CVEs are handled by rebuilding the deb in the monthly release.

### 11.2 Rendered configuration

`/etc/uc-hub/hub.yaml`:

```yaml
site_file: /etc/uc-hub/site.yaml       # seed only; after the first start the hub DB is authoritative
data_dir: /var/lib/uc-hub
listen: {host: 127.0.0.1, port: 8080}
web_dir: /usr/share/uc-hub/web
auth:
  tokens:
    - {user: ilija, roles: [admin], token_env: UC_HUB_TOKEN_ILIJA}
llm: {provider: none}                  # zai: api_key_env UC_HUB_ZAI_API_KEY
drivers:
  bacnet_uc:
    discover_broadcast: [10.20.0.255:1337]
    uc_link_wasm: /usr/share/uc-hub/wasm/uc-link.wasm   # only when the file exists
  bacnet_ip: {interface: 10.20.0.2/24}
  mqtt: {host: 127.0.0.1, port: 1883, username: uc-hub, password_env: UC_HUB_MQTT_PASSWORD, client_id: uc-hub, keepalive_s: 30}
# history: {url: "unix:/run/uc-historian/api.sock", flush_ms: 1000, batch_max: 1000, queue_max: 200000}   # once C9 exists
```

`services.hub.overrides` is merged into `drivers`. `/etc/uc-hub/site.yaml` is the
manifest from `services.hub.manifest` / `uc-ctl hub seed`, or a minimal one:
`{apiVersion: bacnet-uc/v1, kind: Site, metadata: {name: <site.id>}}`.

**Dev-mode guard:** `apply` refuses to enable the proxy role while the rendered
`hub.yaml` has no tokens, because a proxy on the same host defeats the hub's
"no tokens ⇒ loopback only" rule. The health check also alarms when
`/api/health` reports `dev_mode: true`. C14 adds the hub-side refusal.

### 11.3 Unit `uc-hub.service`

```ini
[Unit]
Description=uc-hub site gateway
Wants=mosquitto.service uc-historian.service uc-time-ready.service
After=network-online.target mosquitto.service uc-historian.service uc-time-ready.service
ConditionPathExists=!/etc/uc-controller/hold
StartLimitIntervalSec=0
[Service]
User=uc-hub
Group=uc-hub
SupplementaryGroups=uc-hist-api
ExecStart=/usr/lib/uc-controller/env-from-creds /opt/uc-hub/venv/bin/uc-hub serve -c /etc/uc-hub/hub.yaml
# LoadCredential= lines come from the rendered drop-in uc-hub.service.d/credentials.conf
KillSignal=SIGINT              # until C1: SIGTERM skips services.stop() (lease release, bridge relinquish) [V]
TimeoutStopSec=45
Restart=always
RestartSec=5
StateDirectory=uc-hub
StateDirectoryMode=0700
UMask=0077
MemoryHigh=768M
MemoryMax=1G
OOMScoreAdjust=-600
CPUWeight=200
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
CapabilityBoundingSet=
[Install]
WantedBy=multi-user.target
```

- `Wants=` (never `Requires=`) on the historian: history failures never stop supervision.
- `env-from-creds` exports each file in `$CREDENTIALS_DIRECTORY` as an
  environment variable (`mqtt-uc-hub` → `UC_HUB_MQTT_PASSWORD`,
  `hub-token-<user>` → `UC_HUB_TOKEN_<USER>`, `zai-api-key` → `UC_HUB_ZAI_API_KEY`)
  and `exec`s the hub. It is a bridge until C12.
- `uc-hub mcp` (stdio) cannot run beside `serve` (data_dir lock). Remote MCP
  needs C19.
- Leases and the plant: on stop the hub releases leases and relinquishes
  bridges (with SIGINT). B2 `lease_ms` covers a crash.

---

## 12. Reverse proxy: nginx 1.26.3 (Debian)

nginx is chosen over Caddy 2.6.2 for Debian security support (statically linked
Go packages are rebuilt rarely) and maturity; `nginx -t` fits the apply
transaction. Caddy was tested by research; nginx's SSE and redaction behaviour
is tested in T3.

```nginx
log_format uc_noquery '$remote_addr [$time_iso8601] "$request_method $uri $server_protocol" $status $body_bytes_sent $request_time';
server { listen 80; listen [::]:80; return 301 https://$host$request_uri; }
server {
  listen 443 ssl; listen [::]:443 ssl; http2 on;
  server_name uc-ctl.corp.example 192.0.2.20;
  ssl_certificate /etc/uc-controller/tls/ui.fullchain.pem; ssl_certificate_key /etc/uc-controller/tls/ui.key;
  ssl_protocols TLSv1.2 TLSv1.3;
  add_header Strict-Transport-Security "max-age=31536000" always;
  add_header X-Content-Type-Options nosniff always;
  add_header X-Frame-Options DENY always;
  add_header Referrer-Policy no-referrer always;
  server_tokens off;
  client_max_body_size 20m;
  access_log /var/log/nginx/uc-hub.log uc_noquery;      # $uri has no query string: access_token is never logged
  location = /healthz { alias /run/uc-health/healthz.json; default_type application/json; access_log off; }
  location / {
    proxy_pass http://127.0.0.1:8080; proxy_http_version 1.1;
    proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; proxy_set_header X-Forwarded-Proto https;
    proxy_buffering off; proxy_cache off; proxy_read_timeout 1h;             # SSE
  }
}
```

- `/healthz` is unauthenticated and contains only `{"status","release","time"}`
  for the external dead-man check.
- The historian API and metrics are not proxied.
- Access logs rotate daily, 90 days (logrotate drop-in).
- The UI certificate: `ui.cert: provided` uses secrets `ui-key` and `ui-chain`;
  `site` uses `uc-pki ui` (§9.1).

---

## 13. Secrets

| Secret | Origin | At rest | Consumer | Backup |
|---|---|---|---|---|
| `mqtt-uc-hub`, `mqtt-uc-historian`, `mqtt-uc-health` | generated (32 random URL-safe chars) | `/etc/uc-controller/secrets/` 0600 | `LoadCredential=`; hashes in the broker passwd file | bundle |
| `hub-token-<user>` | generated (43 chars) by `apply` for each `services.hub.users` entry; `uc-ctl token rotate` | same | uc-hub via env-from-creds | bundle |
| `zai-api-key` | `uc-ctl secret set` | same | uc-hub | bundle |
| `backup-ssh-key` (ed25519) | generated | same | uc-backup (SFTP), pgBackRest SFTP repo | bundle |
| `pgbackrest-cipher` | generated | same | pgBackRest `repo1-cipher-pass` | bundle **and** owner/integrator vaults |
| `ui-key`, `ui-chain` | provided by IT, or issued by `uc-pki ui` | same | nginx (installed to `/etc/uc-controller/tls/`, 0600 root) | bundle |
| issuing CA key | office, carried in the initial bundle | `/etc/uc-pki/issuing.key` 0600 | `uc-pki` | bundle |
| broker key | `uc-pki broker` on the controller | `/etc/mosquitto/uc/broker.key` 0600 mosquitto | mosquitto | re-issued on restore |
| SSH host keys | first boot | `/etc/ssh/` | sshd | bundle |
| age recovery identities, root CA key, pgBackRest cipher pass | offline | owner and integrator vaults | restore | – |

- Secrets are **never** in `controller.yaml` or `hub.yaml`.
- `systemd-creds` host-key encryption is not used. It is bound to the machine-id
  and `credential.secret`, so restoring onto a replacement unit needs an extra
  re-encryption step, and it only protects leaked copies, which the
  age-encrypted bundles already protect.
- No full-disk encryption by default: Pi OS does not support an encrypted root,
  the Pi 4 has no AES instructions and no protected key store, and unattended
  recovery after power cuts matters more. Physical protection is a locked
  cabinet. Optional later: LUKS2 `xchacha12,aes-adiantum-plain64` for `/srv/uc`
  with Clevis/Tang.
- Backups are encrypted with **age** (X25519 + ChaCha20-Poly1305, fast on the
  A72) to recipient public keys. The controller cannot decrypt its own backups.

---

## 14. Backup, restore, replace

### 14.1 Site bundle (small, critical)

- **When:** nightly at `backup.at` (`uc-backup.timer`), after every successful
  `apply`, before every update, on demand.
- **Content:** `controller.yaml`, `devices.yaml`, `secrets/`, `/etc/uc-pki`
  (never the root key), SSH host keys, `/etc/uc-hub/site.yaml`, the hub DB via
  `sqlite3 /var/lib/uc-hub/uc-hub.db ".backup ..."` (online-consistent; C17 adds
  `uc-hub backup`), `/var/lib/uc-hub/artifacts` up to 200 MB, `hist.device` +
  `hist.point` + `hist.point_alias` via COPY (keeps point ids stable),
  `board.json`, metadata (versions, dpkg selections, EEPROM config, config hash).
- **Format:** `site-<id>-<utc ts>.tar.zst.age`, encrypted to `backup.recipients`
  (initial bundles: to a one-time identity). Typically a few MB. A bundle may be
  partial (an initial bundle has only PKI and devices); restore installs what
  is present and keeps the rest.
- **Where:** `/srv/uc/backup/bundles` (keep 30; `/var/lib/uc-controller/bundles`,
  keep 3, when p3 is absent); each SFTP target (the server should use
  `ForceCommand internal-sftp -P remove,rename,posix-rename` so a compromised
  controller cannot delete old bundles); the `UCBACKUP` USB stick if present
  (mounted only for the copy).
- Success writes `/var/lib/uc-controller/backup.ok` (mtime is monitored) and a
  `controller.backup` event.

### 14.2 History

- **Nightly history summary (always):** monthly files of `sample_1d`,
  `sample_1h` (last 13 months), `event`, `point` as compressed COPY,
  age-encrypted, copied to the bundle targets. Long-term trends survive even
  without pgBackRest.
- **pgBackRest 2.59.1** when `backup.history_repo` is set: `repo1-type=sftp`,
  `repo1-cipher-type=aes-256-cbc`, `compress-type=lz4`, `archive-async=y`,
  `spool-path=/srv/uc/pgbackrest-spool`, **`archive-push-queue-max=2GiB`** (an
  unreachable NAS drops WAL from the archive instead of filling `/srv/uc` and
  stopping PostgreSQL; alarmed via `pg_stat_archiver`). Full on Sunday 01:30, diff
  daily 01:30. RPO ~1 min while archiving works (`archive_timeout=60`).
- No `pg_dump` of hypertables (needs `timescaledb_pre_restore()` and the same
  extension version, and grows linearly). No file copy of live PG files.
- Risk: pgBackRest was archived and revived in 2026 [R]. Fallback: weekly
  `pg_basebackup -Ft` into the bundle targets.

### 14.3 Restore (`uc-ctl restore`)

1. Create `/etc/uc-controller/hold` (BMS zone closed, hub not started).
2. Stop services.
3. Decrypt and unpack (`--identity FILE`, or `-` to read the identity from stdin).
4. Pin the MAC from `board.json` if `network.mac: auto`.
5. `apply`.
6. Start services, run probes, write a report to `/srv/uc/reports/`.
7. The operator checks and runs `uc-ctl bms up` (removes hold, re-applies the firewall, starts the hub).

`uc-ctl restore-history` runs `pgbackrest restore` (or imports the latest
history summary) into a stopped cluster.

### 14.4 Replace

| Case | Steps | Target time |
|---|---|---|
| Board dead, SSD fine | Move SSD and RTC to the spare. Boot. `uc-boot-gate` sees a new board serial, pins the previous MAC (`network.mac: auto`), raises `controller.board_replaced`. | ~15 min |
| SSD dead or total loss | Flash the same or newer release, copy `controller.yaml` from git to FAT, boot, `uc-ctl restore <latest bundle> --identity <key>`, checks, `uc-ctl bms up`, optionally `uc-ctl restore-history`. | RTO ≤ 2 h with a spare; RPO config/PKI/hub ≤ 24 h and every apply; history ≤ 1 min with pgBackRest, else best-effort |
| Quarterly drill | Restore the latest bundle into the VM tier (T4) with no BMS link; record time and row counts. | – |

---

## 15. Health monitoring

`uc-health.timer` runs `uc-health` every 60 s (Python stdlib + paho). Bounded
restart state lives in `/var/lib/uc-controller/health-state.json`.

| Check | Warn | Crit | Automatic action |
|---|---|---|---|
| supervision units active (mosquitto, uc-hub, chrony, nginx, dnsmasq if enabled) | restart count rising | inactive > 2 min | systemd restarts |
| MQTT loopback round trip on `uc-controller/health/probe` as `uc-health` | 1 failure | 3 failures | restart mosquitto (≤3/h) |
| hub `GET 127.0.0.1:8080/api/health`; `dev_mode` must be false | 1 failure | 3 failures; dev_mode true | restart uc-hub (≤3/h) |
| SNTP query to `bms_ip` | stratum 10 on local reference > 30 min | stratum 0 served, or not served while `time.serve` | – |
| RTC | – | missing or invalid | chrony policy (§7) |
| historian `/v1/health` (db, edition, lag, failed jobs, archive) | lag > 60 s, drops > 0 | PG down > 5 min; historian down | restart historian (≤3/h) |
| disk `/` and `/srv/uc` | 80 % | 90 % | space budget run |
| filesystem read-only; p3 not mounted | – | yes | liveness (§4.6) |
| SMART (`smartctl -d sat -H -A`, cached hourly) | wear > 80 %, reallocated > 0 | failing | – |
| `vcgencmd get_throttled`, SoC temperature | any "occurred" bit; > 70 °C | under-voltage now; > 80 °C | – |
| boot source, boot-loop suppression, board replaced | board replaced | SD boot; suppressed | – |
| certificates and CRL (`uc-pki check`), UI cert | < 1 year (fixed epoch), UI < 30 d | < 30 d, UI < 7 d | – |
| backup age (bundle, pgBackRest) | > 26 h | > 72 h | – |
| devices online (retained status) vs expected | fewer | < 50 % | – |
| journald persistent; pending kernel > 35 d; security updates pending > 3 d | yes | – | – |

**Outputs:**
- `/run/uc-health/health.json` (full) and `/run/uc-health/healthz.json`
  (`{"status":"ok|degraded|critical","release":...,"time":...}`), served at
  `https://<ui>/healthz` for an **external dead-man check** by IT monitoring. A
  dead controller cannot alarm about itself.
- Retained MQTT `uc-controller/health` (JSON), which the hub can show as a device
  and turn into alarms and Web Push (C11).
- Journal entries with a stable `MESSAGE_ID` per alarm (used by `uc-ctl status`).
- Textfile metrics in `/var/lib/uc-controller/metrics/*.prom` (health and
  historian). `prometheus-node-exporter` is an optional role, bound to loopback
  and exposed through nginx basic auth; not installed by default.

---

## 16. Power loss and UPS

- **Without UPS** (default) a power cut is a crash: ext4 journal replay and fsck
  preen, PG crash recovery (at most ~2 GB of WAL; minutes on a Pi 4 [HW]; the
  hub does not wait for it), mosquitto reloads its SQLite store (≤5 s of
  changes lost), hub SQLite in WAL mode stays consistent but may lose its last
  commits (C16 asks for `synchronous=FULL`), FAT is read-only and unaffected.
- Integrity depends on the PLP SSD and a bridge that honours FLUSH; T5 checks it
  with 50 power pulls.
- **With a DC-UPS:** `dtoverlay=gpio-shutdown,gpio_pin=17` produces KEY_POWER and
  logind powers off; no daemon needed. The UPS provides the delay and must cycle
  its output afterwards, or the Pi stays halted (`WAKE_ON_GPIO=1`). Shutdown
  budget < 60 s (hub stop ≤ 45 s, PG fast shutdown). A building UPS can use
  `nut-client` as a secondary instead (optional).

---

## 17. Updates

### 17.1 Release artifacts (CI, native arm64 runner)

`uc-controller_<ver>_all.deb`, `uc-hub_<hubver>+uc<N>_arm64.deb`,
`uc-controller-<ver>-pi4.img.xz` (+ sha256, SBOM, file manifest),
`uc-update-<ver>.tar` (a signed flat apt repo with our debs and the Debian, RPi and
third-party packages tested with this release; the release key ships as
`/usr/share/keyrings/uc-controller-release.gpg`). Versions are CalVer
`YYYY.MM.PATCH` (`controller/VERSION`).

### 17.2 Automatic security updates (online sites)

`unattended-upgrades` daily at 03:30 (timer drop-ins), origins Debian-Security,
Debian and Raspberry Pi Foundation (OpenSSL and glibc fixes arrive as `+rpt`
builds from archive.raspberrypi.com [V]). Blacklist `linux-image-`,
`linux-headers-`, `raspi-firmware`, `rpi-eeprom`. No automatic reboot.
needrestart `$nrconf{restart} = 'a'`. Third-party repos (PGDG, Timescale,
mosquitto) are **not** in the automatic origins.

### 17.3 Maintenance update (`uc-ctl update`)

One mechanism for online and offline sites:
1. Pre-flight: free space, not in hold, current health green (or `--force`).
2. Pre-update site bundle; snapshot the hub DB (`sqlite3 .backup`) and the
   mosquitto store into `/var/lib/uc-controller/snapshots/<from>-<to>/`; keep the
   current uc-* debs in `/var/lib/uc-controller/releases/` (last 2).
3. Stop the historian (the hub buffers). `apt-get install` from the bundle's
   local repo (or online with pins). `--with-kernel` also updates kernel,
   raspi-firmware and rpi-eeprom (FAT remounted read-write by the apt hook).
4. Restart affected services (reboot when the kernel changed), run all probes.
5. **Fail:** `uc-ctl rollback` reinstalls the previous uc-* debs and restores
   the hub DB and mosquitto snapshots if their schema changed (the hub
   auto-migrates on open and refuses a newer DB [V]). OS packages roll forward only.
6. **Pass:** post-commit steps: `ALTER EXTENSION timescaledb UPDATE` and the
   toolkit (first statement of a fresh session), `uc-historian migrate
   --post-commit`, delete snapshots after 7 days, `controller.update` event.

Irreversible state changes run only after the health gate. This is what keeps
rollback possible. A PostgreSQL major upgrade (18 → 19) is a separate
runbook (`pg_upgradecluster` with both TimescaleDB builds installed).

### 17.4 Security-driven out-of-window updates

mosquitto listens on the BMS VLAN, so its security advisories trigger an
immediate `uc-ctl update`. PostgreSQL is socket-only, so a monthly cadence is
acceptable.

### 17.5 v2: A/B root

After T5 has shown Pi 4 tryboot from a USB SSD (`autoboot.txt`,
`tryboot_a_b=1`, `[boot_partition=N]` cmdline selection): GPT layout with
`bootconfig`, `boot_a/b`, `system_a/b`, `persistent` (the rpi-image-gen
`image-rota` names), RAUC 1.13 with a custom tryboot backend, read-only root,
health gate before commit. RAUC signing and install into file-backed slots were
verified in a container [V]. rpi-image-gen's own slot tooling does not handle
`/dev/sdX` yet [V]. Migration from v1 is reflash + restore.

---

## 18. Security summary

- **Accounts:** one account per person from `admins` (group `ssh-admins`), keys
  in `/etc/ssh/authorized_keys/%u` (root-owned, rendered), sudo with a password.
  Optional `ssh_user_ca` for short-lived SSH certificates. The first-boot user
  is removed. Admins are not in `video` (that would expose the OTP via `/dev/vcio`).
- **sshd** (`/etc/ssh/sshd_config.d/10-uc.conf`): `PermitRootLogin no`,
  `PasswordAuthentication no`, `KbdInteractiveAuthentication no`,
  `AuthenticationMethods publickey`, `AllowGroups ssh-admins`, `MaxAuthTries 3`,
  `LoginGraceTime 20`, `PerSourcePenalties yes`, `ClientAliveInterval 300`,
  `ClientAliveCountMax 2`, no X11/agent/TCP forwarding, no tunnels,
  `LogLevel VERBOSE`. No `ListenAddress` (sshd may start before the VLAN is up);
  nftables restricts.
- **Network:** default-drop in, out and forward; no routing; hold mode; fail-safe ruleset.
- **Remote access:** none on the box by default. Optional WireGuard, outbound
  only, switched on by the owner (later). `rpi-connect-lite` is purged.
- **Logs:** device logs in the history DB (180 d); journal 1 year; nginx 90 d;
  hub audit (its DB) ≥ 1 year. Device text is stored, never interpreted.
- **BSI INF.14 / IEC 62443:** zones and no forwarding (A6), autonomy of the
  plant (A22), synchronous time (A24), monitoring (A25), logging (A26), physical
  separation option `dual` (A28), GNSS time option (A30).
- **EU CRA:** Art. 14 reporting applies since 2026-09-11 if uc-controller is
  placed on the market as a product; the image SBOM and a vulnerability intake
  process are part of the release process [R].

---

## 19. Repository layout and code interfaces

### 19.1 Tree (owner = work package, §23)

```
controller/
  README.md, Makefile                                   WP7
  VERSION                                               WP6
  docs/DESIGN.md                                        (this file)
  docs/REQUESTS.md                                      WP7
  docs/HISTORIAN-API.md, docs/DATA-MODEL.md, docs/PERFORMANCE.md   WP3
  docs/RUNBOOK-operations.md, docs/RUNBOOK-replace-restore.md, docs/ACCEPTANCE.md   WP5
  docs/RUNBOOK-install.md                               WP6
  site/controller.schema.json, site/examples/{trunk,dual,flat}.yaml   WP1
  src/uc_controller/                                    Python package of uc-ctl
    __init__.py __main__.py cli.py config.py model.py render.py validate.py
    apply.py secrets.py runner.py paths.py sdnotify.py hold.py testing.py   WP1
    roles/__init__.py roles/base.py                     WP1
    roles/network.py roles/ssh.py roles/time.py roles/dhcp.py       WP1
    commands/{apply,render,secret,bms,time}.py          WP1
    roles/os.py roles/boot.py                           WP6
    platform/{firstboot,partition,bootgate,liveness,eeprom,board,hwchecks}.py   WP6
    commands/{firstboot,eeprom}.py                      WP6
    roles/broker.py pki.py devices.py commands/{pki,device}.py      WP2
    roles/postgres.py roles/historian.py commands/history.py        WP3
    roles/hub.py roles/proxy.py commands/{hub,token}.py WP4
    roles/ops.py ops/{health,backup,restore,update,status,selftest,support}.py
    commands/{status,backup,restore,update,selftest,support}.py     WP5
  templates/<role>/...                                  owned by the role's WP
  rootfs/...                                            static files; each file owned by one WP (§23)
  pki/uc-pki, pki/openssl-uc.cnf                        WP2
  historian/                                            WP3 (package uc_historian, migrations, tests)
  hub-pkg/                                              WP4
  hardware/acceptance.sh                                WP5
  packaging/repos/ (sources, keyrings, FINGERPRINTS, uc-controller.pref, add-repos.sh)   WP1
  image/, packaging/ (everything else), rootfs/usr/sbin/uc-ctl                         WP6
  tests/lib/                                            WP1 (container helpers, shared by all)
  tests/core/  tests/broker/  tests/historian/  tests/hub/  tests/ops/  tests/platform/  tests/e2e/
                                                        WP1  WP2  WP3  WP4  WP5  WP6  WP7
```

Installed layout: `uc_controller` and `uc_historian` →
`/usr/lib/python3/dist-packages/`; templates → `/usr/share/uc-controller/templates/`;
schema → `/usr/share/uc-controller/controller.schema.json`; migrations →
`/usr/share/uc-controller/historian/migrations/`; `uc-ctl`, `uc-pki` →
`/usr/sbin/`; helpers → `/usr/lib/uc-controller/`; units →
`/usr/lib/systemd/system/`. `paths.py` finds resources in the source tree when
`UC_CONTROLLER_SRC` is set (tests) and in the installed locations otherwise.

### 19.2 Role interface (`uc_controller/roles/base.py`, owned by WP1)

Every configurable service is a **role**. `roles/__init__.py` lists the role
modules in order and imports them lazily; a missing module is skipped with a
warning, so work packages can land independently.

```python
ROLE_MODULES = ["os", "boot", "network", "ssh", "time", "dhcp",
                "broker", "postgres", "historian", "hub", "proxy", "ops"]

@dataclass(frozen=True)
class RenderedFile:
    path: str                  # absolute target path, e.g. "/etc/mosquitto/mosquitto.conf"
    content: bytes
    mode: int = 0o644
    owner: str = "root"
    group: str = "root"
    secret: bool = False       # content masked in diffs and logs

@dataclass(frozen=True)
class Validator:
    name: str
    argv: list[str]            # "{staged}" is replaced by the staging root
    needs: str | None = None   # executable; validator skipped with a warning if absent
    ok_codes: tuple[int, ...] = (0,)
    timeout_s: float = 30.0

@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str = ""

@dataclass(frozen=True)
class Probe:
    name: str
    run: Callable[["RenderContext", "Runner"], ProbeResult]
    retries: int = 3
    delay_s: float = 2.0

class Role:
    name: str                              # "broker"
    order: int                             # install/reload order, lower first
    units: dict[str, str] = {}             # unit -> "reload" | "restart" | "try-restart"
    def enabled(self, ctx: "RenderContext") -> bool: return True
    def render(self, ctx: "RenderContext") -> list[RenderedFile]: ...
    def validators(self, ctx: "RenderContext") -> list[Validator]: return []
    def prepare(self, ctx: "RenderContext", run: "Runner") -> None: ...        # idempotent setup before install (users, dirs, clusters)
    def post_install(self, ctx: "RenderContext", run: "Runner") -> None: ...   # after files are installed, before reload
    def probes(self, ctx: "RenderContext") -> list[Probe]: return []
    def enable_units(self, ctx: "RenderContext") -> dict[str, bool]: return {} # unit -> enabled
    def unit_actions(self, changed: list[str]) -> dict[str, str]:              # changed target paths -> unit actions
        return dict(self.units) if changed else {}
```

`RenderContext` fields: `site` (validated `controller.yaml`, nested dataclasses),
`devices` (`devices.yaml`), `derived` (§5.1 table), `secrets` (`get(name)`,
`ensure(name, kind)`, `path(name)`), `tpl` (`render(name, **vars) -> str`,
Jinja2 with `StrictUndefined`, templates under `templates/`), `prefix` (""
for install, the staging root for validation; templates write every absolute
path as `{{ P('/etc/...') }}`), `facts` (multiarch triplet, mosquitto version,
PG major, `/dev/rtc0` present, container or not; faked in tests), `first_boot`,
`paths`.

`Runner` (`runner.py`): `run(argv, check=True, input=None) -> CompletedProcess`,
`systemctl(action, unit)` (no-op and logged with `--no-systemd`),
`as_user(user, argv)`. Tests use `FakeRunner`, which records calls.

### 19.3 Command modules

`cli.py` imports `COMMAND_MODULES` lazily, like roles. Each module defines
`register(subparsers) -> None` and sets a handler. Exit codes: 0 ok, 1 error,
2 validation refused, 3 probes failed and rolled back.

### 19.4 Interfaces between work packages

| Interface | Provider | Consumers | Contract |
|---|---|---|---|
| `uc_controller.roles.base` (Role, RenderContext, RenderedFile, Validator, Probe) | WP1 | WP2–WP6 | §19.2 |
| `uc_controller.runner.Runner`, `FakeRunner` | WP1 | all | `run`, `systemctl`, `as_user`; `FakeRunner.calls` records argv lists; scripted results per argv prefix |
| `uc_controller.testing` | WP1 | all role tests | `context(example="trunk", prefix="", facts=None, secrets=None) -> RenderContext` built from `site/examples/<example>.yaml` with deterministic fake secrets; `render_role(name, example) -> dict[path, bytes]`; `assert_golden(files, golden_dir)` (rewrites when `UC_UPDATE_GOLDEN=1`) |
| `uc_controller.secrets.SecretStore` | WP1 | all | `get(name)`, `ensure(name, kind)` with kinds `password`, `token`, `hex32`, `ssh-ed25519`; `set(name, value)`; `path(name)`; `names()` |
| apply hook `uc_controller.ops.backup.after_apply(ctx, runner)` | WP5 | WP1 `apply` step 10 | optional; import failure is ignored |
| `uc_controller.pki` | WP2 | WP4 (UI cert), WP5 (health, bundle) | `issue_ui(ctx, runner) -> (cert_path, key_path)`, `check() -> list[dict]` (`file`, `subject`, `not_after`, `days_left`), `import_dir(src)`; PKI dir `/etc/uc-pki` |
| `uc_controller.devices` | WP2 | WP1 (dhcp role), WP5 (health) | `load() -> list[Device]` (`id`, `hwid`, `name`, `mac`, `ip`, `status`); `active()`; file `/etc/uc-controller/devices.yaml` |
| `uc_historian.client` | WP3 | WP5 (health, backup), WP7, `uc-ctl event` | stdlib-only UDS HTTP client: `health() -> dict`, `event(kind, data, severity=1)`, `series(...)`, `logs(...)`, `events(...)`; socket path from `UC_HISTORIAN_SOCKET` or `/run/uc-historian/api.sock` |
| `/v1` HTTP API | WP3 | hub (C2–C9), WP5, WP7 | §10.7–10.15 and `controller/docs/HISTORIAN-API.md` (authoritative, versioned) |
| `uc_controller.platform.hwchecks` | WP6 | WP5 (`selftest --hardware`) | functions returning `CheckResult(name, ok, detail)`: `uas()`, `trim()`, `smart()`, `throttled()`, `rtc()`, `eeprom()`, `boot_source()`, `partuuid_duplicates()`; each takes a `Runner` |
| pgBackRest enable flag | WP5 (renders `/etc/pgbackrest/pgbackrest.conf` and `/etc/uc-controller/pgbackrest.enabled`) | WP3 (`pg-archive` wrapper) | wrapper runs `pgbackrest --stanza=uc archive-push %p` only when the flag exists, else exits 0 |
| MQTT device simulator `tests/broker/sim/mqtt_device.py` | WP2 | WP3 (optional), WP7 | class `MqttTlsDevice(id, host, port, ca, cert, key, root="bacnet-uc", telemetry_s=10)` with `start()`, `stop(clean=False)`, `log(lvl, src, msg)`; publishes per the mqtt_tls README |
| Hub shim `tests/historian/sim/hub_shim.py` | WP3 | WP7 | reference implementation of C2/C3: subscribes to `<root>/+/telemetry` and `status` on 1883 as `uc-hub`, maps to refs `<site>/<client id>/telemetry.<field>` and `/status`, `PUT /v1/points`, `POST /v1/readings` |
| Syslog simulator `tests/historian/sim/syslog_device.py` | WP3 | WP7 | RFC 5424 sender with `bacnet-uc-<mac>` hostnames, floods, malformed lines |
| Test container helpers `tests/lib/` | WP1 | all | `ctr.sh`: `ctr_build DOCKERFILE TAG [CONTEXT]`, `ctr_run TAG CMD...` (repo mounted read-only at `/src`, `UC_CONTROLLER_SRC=/src/controller`, `--network host`, `UC_TEST_PLATFORM`); base images `uc-test-trixie` and `uc-test-raspios` |
| Package repos `packaging/repos/add-repos.sh` | WP1 | WP2–WP7 | adds PGDG, Timescale and mosquitto repos with pinned keys and `uc-controller.pref`; idempotent; honours `APT_HTTPS_PROXY` |

System users and groups (created by `sysusers.d`, WP6): users `uc-hub`,
`uc-historian`, `uc-health`; groups `uc-hist-api` (members `uc-hub`, `uc-health`)
and `ssh-admins`. `postgres` and `mosquitto` come from their packages.

### 19.5 Well-known paths (`paths.py`)

All paths in §5.7 plus: `/var/lib/uc-controller/{staging,lkg,installed.json,firstboot,boot-history.jsonl,board.json,health-state.json,backup.ok,releases,snapshots,metrics}`,
`/run/uc-controller/{chrony-local.conf,liveness-suppressed,time-untrusted}`,
`/run/uc-health/`, `/run/uc-historian/`, `/etc/uc-controller/hold`.
Every path is overridable with `UC_ROOT=<dir>` for tests (prefix applied to all).

---

## 20. Test strategy

| Tier | Where | What |
|---|---|---|
| **T0 static** | host or container | `shellcheck`, `python3 -m compileall`, `pyflakes`, `sh -n`; schema validation of `site/examples/*.yaml` |
| **T1 unit** | host Python ≥ 3.11 or a native `debian:trixie` container with Debian `python3-*` and `python3-pytest` | renderer golden tests per role and example (trunk, dual, flat); config validation; derived values; apply state machine with `FakeRunner` (LKG, rollback, confirm); client-id vectors; storage policy; LOCF buckets; tier routing; syslog parser incl. injection strings; boot gate; time policy decision table; health checks with stubs |
| **T2 validate** | arm64 Pi OS rootfs container (`raspios-lite:2026-09-15`, imported from the official image with its sha256 checked) with the release package set | install the debs (dependency closure, postinst idempotence, purge); `uc-ctl apply --no-systemd` for each example; all validators; `systemd-analyze verify` and `systemd-analyze security --offline=true` (exposure ≤ 2.5 for our units); `udevadm verify` |
| **T3 functional** | arm64 trixie or Pi OS containers; daemons in the foreground, no systemd | PKI chain and `openssl verify -attime`; broker matrix (mTLS, CN = client id, cross-device publish dropped, CRL, no-cert, hub wildcard discovery, historian ACL, oversize); PostgreSQL + TimescaleDB: migrations, policies via `run_job`, read-only role, idempotent ingest incl. compressed chunks; historian with simulated devices, syslog and a hub shim, PG outage without acknowledged loss, broker restart, SIGKILL mid-batch, clock step; chrony policies probed with an SNTP client (stratum/LI); nginx SSE unbuffered and token not logged; bundle round trip and pgBackRest to a posix repo |
| **T4 system** | `qemu-system-aarch64 -M virt -cpu cortex-a72 -smp 4 -m 3072` (TCG) in an amd64 trixie container; golden rootfs plus Debian `linux-image-arm64` and an initramfs with `MODULES=most` on the real p1/p2 layout (recipe verified: boots to multi-user with systemd 257 in ~4 min [V]) | first boot from a test `uc-controller.yaml` (p3 created), `systemctl is-system-running`, `uc-ctl status`; nightly scenarios: power cut (SIGKILL qemu during ingest, then offline `e2fsck -fn` and `pg_amcheck`), p3 missing, liveness (detach disk via QMP), apply rollback and confirm timeout, restore drill in hold mode, update and rollback, RTC-invalid clock (`-rtc base=2020-01-01`), disk full |
| **T5 hardware** | real Pi 4 + BOM, per bridge/SSD batch and per release; `uc-ctl selftest --hardware` + `docs/ACCEPTANCE.md` | 1 `lsusb -t` uas at 5000M, no resets in dmesg; 2 `sg_vpd -p lbpv` LBPU=1 → bridge added to `bridges.allow`, `fstrim -v`; 3 `smartctl -d sat -a`; 4 24 h fio with fsync + **50 power pulls**, then `e2fsck` and `pg_amcheck` clean; 5 `get_throttled`=0x0 under load, cabinet thermals; 6 bootloader ≥ 2026-05-17, EEPROM config; 7 **gated items**: `BOOT_WATCHDOG_TIMEOUT`, `kernel_watchdog_timeout` (boot times with USB rootwait, SSD pulled at boot); 8 SSD pulled at runtime → liveness reboot; 9 802.1Q trunk 72 h on the target switch, `dual` mode; 10 RTC hctosys, battery removed (invalid policy); 11 bacpypes3 bound to `bms_ip/prefix` receives I-Am broadcasts; 12 UPS shutdown and output cycle; 13 performance targets §10.17 |
| **E2E** | arm64 Pi OS container with the installed debs | §23 WP7 |

**Container conventions** (this environment has an intercepting HTTPS proxy):
- Build and run containers with `--network host`. Pass `HTTPS_PROXY` as a build
  argument. Copy `/root/.ccr/ca-bundle.crt` into the build context as
  `extra-ca.crt` when it exists and install it in the test image only.
- In test images set `Acquire::https::Proxy` and rewrite
  `http://deb.debian.org` and `http://archive.raspberrypi.com` to `https://`.
- The golden image build fails if the proxy CA appears anywhere in the tree.
- `UC_TEST_PLATFORM` defaults to `linux/arm64`; `linux/amd64` is allowed for fast
  iteration in T1 and T3 (the same package versions exist for amd64). T2, T4 and
  E2E run arm64 only.
- systemd as PID 1 does not work in a qemu-user container (units fail with
  `243/CREDENTIALS` [V]). Containers start daemons in the foreground; unit
  runtime behaviour is covered by T4.
- Emulated timings are relative only. Assertions use generous limits; absolute
  targets live in T5.

CI: T0–T3 on every push (native arm64 runner avoids qemu-user); T4 nightly;
T5 per batch and per release.

---

## 21. Requests to other sessions

These are mirrored in `controller/docs/REQUESTS.md` and should be added to
`docs/SESSION_NOTES.md` when that file is next edited. That file also still says
the gateway is a "Pi 5 / CM5 or x86 IPC"; the Pi 4 differences that matter are:
no crypto extensions (ECDSA and ChaCha preferred), no RTC (an add-on RTC is
required before serving M7 SNTP), no native NVMe (USB-SATA with PLP), and a
1.2 A USB budget.

### Harness session (`claude/ai-harness-building-control-05nrgm`), by priority

| # | Request | Why | Controller workaround until then |
|---|---|---|---|
| C1 | Handle SIGTERM like SIGINT so `services.stop()` runs (lease release, bridge relinquish, history flush). | Verified defect: SIGTERM exits 143 without stopping [V]. | `KillSignal=SIGINT`, `TimeoutStopSec=45` |
| C2 | **History sink:** a non-blocking `site.add_listener` callback for readings of trended points of manifest devices; bounded queue (200 000) that coalesces to the newest reading per point on overflow and counts `dropped`; a task POSTs `/v1/readings` every `flush_ms` or `batch_max` over httpx `AsyncHTTPTransport(uds=...)` with session id and seq; retry with backoff on 503 or connection errors; flush for up to 5 s on stop. | Durable history with no DB dependency in the hub; httpx is already a dependency; `_publish` does not change. | history only in the in-memory deque |
| C3 | **Registry push:** `PUT /v1/points` snapshot (devices with a stable `key`, points with datatype, units, states, tags, space, trended, history overrides) at start, on a live revision and on a description change. | Datatype and units for queries; series continuity across renames. | – |
| C4 | Manifest `history:` section in `site.schema.json`: include/exclude patterns over point ids (tagged points stay included), per-point `{deadband, min_interval_s, heartbeat_s}`, trended-only points polled at `history.poll_interval_s` (default 10 s). | Today only tagged points are recorded; site-wide trending needs a selector; slower polling spares the nodes. | – |
| C5 | `point_history` backed by `GET /v1/series`: `start`/`end` or `minutes|hours|days` up to 10 years; tier, bucket, gaps and coverage in the summary; fallback to the deque with a note. | Long-range trends. | – |
| C6 | New read tools `history_stats`, `device_logs`, `events_query` with fenced, capped device text. | Logs and timeline for troubleshooting; prompt-injection hygiene. | – |
| C7 | HTTP `GET /api/history`, `/api/logs`, `/api/events` (viewer+) proxied to the historian; web Trends view joining `/api/live`. | UI trends beyond 24 h. | – |
| C8 | Event push `POST /v1/events` for writes, relinquish, force/release, lease expiry, bridge start/stop, plan stages, approvals, alarms, hub-seen online/offline, test runs; `actor` = user or `run:<id>`. | One timeline next to the trends. | – |
| C9 | hub.yaml `history: {url, timeout_s, flush_ms, batch_max, queue_max, enabled}` (config.py forbids unknown keys). | Controller-rendered config. | key left out |
| C10 | MQTT timestamps: use M7 `ts` when within ±300 s of the hub clock; drop telemetry whose `seq` is not newer within one boot; later a persistent session with a fixed id. | Correct trend time; no duplicates on redelivery. | – |
| C11 | `/api/health` detail: version, DB schema version, per-driver device counts, broker link, history queue/dropped/last_ok; a host-health device from `uc-controller/health` (MQTT) with alarms and Web Push. | Operators see controller health where they look. | health JSON on MQTT and `/healthz` |
| C12 | Secrets from files (`*_file` keys or `$CREDENTIALS_DIRECTORY/<name>`); minimal environment for subprocesses (clang). | Environment variables leak to children. | `env-from-creds` |
| C13 | `uc-hub check -c hub.yaml` (validate without starting or taking the lock) and a JSON Schema export of hub.yaml. | Transactional apply. | load the config model from the venv |
| C14 | Refuse dev mode when requests carry `X-Forwarded-For` or a non-loopback `Host`, or require an explicit `--dev`. | A same-host proxy defeats "loopback only". | apply refuses the proxy without tokens |
| C15 | `sd_notify` READY=1 after start and WATCHDOG=1 from the event loop when `$NOTIFY_SOCKET`/`$WATCHDOG_USEC` are set. | Detect a hung hub without polling. | health probe restarts |
| C16 | SQLite `synchronous=FULL` (at least for audit and lease writes). | Power cuts are normal on this platform. | – |
| C17 | Compatible, deferrable DB migrations (`min_compatible_version`, `uc-hub db --check/--migrate`); `uc-hub backup --out` via the SQLite online backup API. | Rollback after a failed update without restoring a snapshot. | pre-update snapshot + restore |
| C18 | Standby/read-only start mode: no device writes, no bridges, no leases. | Restore drills with the UI visible. | hub not started in hold |
| C19 | MCP over streamable HTTP at `/mcp` with bearer auth. | stdio MCP cannot coexist with `serve`. | no MCP on the controller |
| C20 | Pin `mcp>=2`; publish a hash-locked requirements file, the wheel and `web/dist` per commit as CI artifacts; update SITE.md's `client_id` example to `z` + base32. Device-certificate signing only through a narrow controller helper behind an approval tier. | Reproducible `uc-hub` deb; CA keys stay out of the AI process. | lock generated in `hub-pkg/` |
| C21 | Later: BACnet/IP TrendLog backfill with ReadRange after hub outages (`src=backfill`, rate-limited); optional BACnet `UTCTimeSynchronization` sender. | Fill gaps; time for nodes without SNTP. | – |

### MQTT firmware (`claude/inter-session-communication-h989ye`)

| # | Request | Why |
|---|---|---|
| M8 | Log `client id z..., hwid ..., mac ...` at boot, before networking. | A device without a certificate cannot connect; its id must be readable on the UART to issue one. |
| M9 | A 32-bit random `boot` id per boot in `info`, telemetry and M6 log entries, and a per-boot `seq` in log entries. | Exact dedupe of live and fetched log lines; exact boot detection. |
| M10 | With M7: a TLS verify callback (`CONFIG_NET_SOCKETS_TLS_CERT_VERIFY_CALLBACK`) that tolerates `FUTURE`/`EXPIRED` while SNTP has not set the clock. Keep `APP_MQTT_BROKER_HOSTNAME` accepting an IP together with `APP_MQTT_TLS_HOSTNAME`. | A controller with a failed RTC must not strand devices; DNS-less broker addressing. |

M6 and M7 are unchanged; M7's `ts` should also appear in log entries.

### BACnet firmware (`claude/zephyr-bacnet-stm32-162k1g`)

- **B2 `lease_ms` is a hard dependency of R1** (existing request): without it,
  forces and writes persist while the controller is down.
- B5 (optional): RFC 5424 STRUCTURED-DATA `[uc@<PEN> hwid="..." boot="..."]` in
  syslog, and document the TIMESTAMP used before time sync.
- Note: the firmware's native_sim build fails on aarch64 (WAMR Kconfig defaults
  to X86_64) [V]; a host-arch-aware default is needed before any sim runs on a Pi.

### HIL rig (`claude/hardware-in-loop-testing-x74tww`)

| # | Request | Why |
|---|---|---|
| HIL-C1 | A controller bench slot: switched 5.1 V/3 A supply (relay), USB-UART on GPIO14/15, the approved SSD and bridge, a trunk port with VLANs 10 and 20. | Tier T5 power-pull, liveness and watchdog tests every night. |
| HIL-C2 | Allow `broker.hil.lan`, NTP and DHCP to be served by a uc-controller instead of the rig host's services. | Tests the controller against the frozen FW contract. |

---

## 22. Risks and open questions

| Risk | Impact | Mitigation / status |
|---|---|---|
| Bridge or SSD ignores FLUSH, or UAS misbehaves | corruption after power cuts | PLP SSD, ASM1153E/ASM235CM only, T5 power pulls per batch, PG data checksums, TRIM allow-list |
| SSD vanishes (VL805, cable, power) | box alive but useless | liveness `reboot-immediate`, boot-loop limiter, under-voltage alarm, 3 A supply |
| TimescaleDB TSL for a distributed image | legal | read-only agent role (technical half of TSL 3.10(iii)); legal review; Apache fallback behind the same API |
| Third-party repos (PGDG, packagecloud, mosquitto) | supply chain, edition flip | pinned keys and packages, edition guard, updates only through tested release bundles |
| Hub requests C1–C9 not landing | no long-range point trends | logs, status, events and syslog work without them; historian ships first with a hub shim so the contract is fixed |
| Clock integrity | wrong timestamps, TLS | mandatory RTC, runtime chrony policy, clock-step events, future guard, fixed-epoch certificates |
| GENET 802.1Q link flap (2020 report) | IT or BMS link loss | T5-9, EEE off, `dual` mode |
| Watchdog EEPROM/firmware settings untested | spurious resets or a coverage gap | gated behind T5-7 |
| bacpypes3 bound to a unicast address may miss broadcasts | BACnet discovery | T5-11; if confirmed, request a hub bind option |
| 4 GB memory pressure | OOM | per-unit limits, OOM order, zram, budget alarms; 8 GB if T5 is close |
| Deadband defaults hide short spikes | missing detail | documented per unit, overridable per point |
| pgBackRest continuity (revived 2026) | history backups | history summary always; `pg_basebackup` fallback |
| Shared PARTUUID across units | wrong mount with two SSDs | never attach two; selftest warns |
| Recovery identities lost | backups unusable | two vault holders; drill verifies decryption |
| Controller is a single point of failure for supervision | UI and history lost while down | by design (R1); cold spare; dead-man check |

**Open questions for the user:**
1. TimescaleDB TSL: acceptable for per-site installs by the integrator, and is a
   legal review planned before shipping preinstalled images? Otherwise the
   Apache fallback (≈90 days raw history) becomes the default.
2. Retention defaults: raw 400 days, hourly and daily rollups forever, device
   logs 180 days, events 10 years?
3. RAM: 4 GB default, 8 GB above ~3000 trended points?
4. Default network per customer: VLAN trunk on the onboard port, or a second
   USB NIC for a physically separate BMS network?
5. Who holds the site root CA (integrator, owner, both), and will owners provide
   an SFTP target for backups and upstream NTP?
6. Is a fleet update server (v2: A/B root, signed desired state) wanted, or are
   USB/SSH updates enough for the first sites?

---

## 23. Work packages

| WP | Title | Owns | Depends on |
|---|---|---|---|
| WP1 | uc-ctl core, network, SSH, time, DHCP | `src/uc_controller/{cli,config,model,render,validate,apply,secrets,runner,paths,sdnotify,hold}.py`, `roles/{__init__,base,network,ssh,time,dhcp}.py`, `commands/{apply,render,secret,bms,time}.py`, `templates/{network,ssh,time,dhcp}/`, `site/`, `testing.py`, `packaging/repos/`, `rootfs/` files for time/dhcp/nftables fail-safe, `tests/lib/`, `tests/core/` | – |
| WP2 | Broker, PKI, device registry | `pki/`, `roles/broker.py`, `pki.py`, `devices.py`, `commands/{pki,device}.py`, `templates/broker/`, mosquitto drop-in, `uc-pki-maint.*`, `tests/broker/` | WP1 |
| WP3 | Historian and PostgreSQL | `historian/`, `roles/{postgres,historian}.py`, `commands/history.py`, `templates/{postgres,historian}/`, historian/PG units and helpers, `docs/{HISTORIAN-API,DATA-MODEL,PERFORMANCE}.md`, `tests/historian/` | WP1 |
| WP4 | uc-hub hosting and reverse proxy | `hub-pkg/`, `roles/{hub,proxy}.py`, `commands/{hub,token}.py`, `templates/{hub,proxy}/`, `uc-hub.service`, `env-from-creds`, nginx drop-in, `tests/hub/` | WP1 |
| WP5 | Operations: health, backup, restore, update | `roles/ops.py`, `ops/`, `commands/{status,backup,restore,update,selftest,support}.py`, `templates/ops/`, health/backup units, `hardware/`, runbooks, `tests/ops/` | WP1 |
| WP6 | Platform and image | `roles/{os,boot}.py`, `platform/` (incl. `hwchecks.py` used by `uc-ctl selftest --hardware`), `commands/{firstboot,eeprom}.py`, `templates/{os,boot}/`, platform units and static OS files, `packaging/` (except `repos/`), `image/`, `rootfs/usr/sbin/uc-ctl`, sysusers/tmpfiles, `VERSION`, `docs/RUNBOOK-install.md`, `tests/platform/`, `tests/vm/` | WP1 |
| WP7 | End-to-end integration | `tests/e2e/`, `README.md`, `Makefile`, `docs/REQUESTS.md` | WP1–WP6 |

Build order: WP1 first (it fixes the role, command and path interfaces), then
WP2–WP6 in parallel, then WP7. v1 is done when WP7's end-to-end test passes on
arm64 and T5 has run once on a real Pi 4.

---

## Appendix A. Conflicts between the candidate designs and how they were resolved

| Topic | Appliance | Fleet | Data | Decision and reason |
|---|---|---|---|---|
| Base / build | golden image from the official image | mmdebstrap from a lock, own package pool | stock image + cloud-init + provisioner | **Golden image from the official image**, plus a lab path with the same debs. A self-hosted pool and mmdebstrap are v2 work; SOURCE_DATE_EPOCH, SBOM, manifest and the proxy-CA check are taken from the fleet design. |
| Partitions | MBR, 16 GiB root, data p3 | GPT A/B + persistent | MBR, 32 GiB root | **MBR, 32 GiB root grown at first boot, p3 data.** A/B is v2 after T5 proves tryboot from USB. |
| Hub data location | root | persistent | `/srv/uc` | **root**, so supervision survives loss of p3. |
| BOOT_ORDER | 0xf41 + alarm | 0xf14 | 0xf14 | **0xf14.** A forgotten SD card would silently replace production; rescue works by unplugging the SSD. |
| rpi-eeprom-update | kept, FAT remount drop-in | masked | kept | **masked**; bootloader only changes in a window. |
| Watchdog EEPROM/firmware keys | gated | set | `kernel_watchdog_timeout=15` | **gated by T5-7.** 15 s may be shorter than USB root discovery; larger values are unverified. |
| Network stack | systemd-networkd | NM keyfiles | netplan + NM | **systemd-networkd**: static appliance config, fewer daemons, same in the VM tier. |
| Broker version | Debian 2.0.21 | 2.1.2 | 2.1.2 | **2.1.2 pinned**: date-check override, incremental persistence (no 300 s snapshot loss), plugin syntax that survives 3.0, HIL parity. |
| Hub ACL | narrowed to topic roots | `readwrite #` | `readwrite #` | **`readwrite #`**: generic-json devices use arbitrary topics; the hub is the trusted supervisor. |
| Certificate validity | fixed epoch 2020–2051 | rotation, CRL +2 y weekly | rotation, CRL +2 y weekly | **Fixed epoch** for device-facing material, CRL regenerated only on revocation: an expired CRL or broker certificate can lock out a whole offline site, and devices check neither dates nor revocation today. UI certificate rotates. |
| Issuing CA key origin | office, in the initial bundle | generated on the box, CSR signed offline | office | **Office**: device certificates are needed for firmware builds before the controller exists. |
| History store | plain PG 17 + own rollups | PG 18 + TSL, hub writes | PG 18 + TSL, historian single writer | **PG 18 + TimescaleDB TSL behind the historian API**: 10–20× less disk and backup volume, verified rollups; the licence risk is isolated behind `/v1` with an Apache fallback. |
| Hub → history write path | hub publishes over MQTT | hub writes PG | HTTP on a Unix socket | **HTTP on a Unix socket**: no broker dependency for sites without MQTT, no new hub dependency (httpx), request/response for queries anyway. |
| History read path | hub calls a SQL function | hub SQL | `/v1` API | **`/v1` API**: the hub never gets DB credentials or schema coupling. |
| Deadband / heartbeat | archiver, from meta topic | – | historian, from registry | **historian**, overrides from the manifest via C3/C4. |
| Retention on disk pressure | drop oldest raw by space budget | – | never delete; pause at 95 % | **Both**: space budget for raw samples and logs (event + warning), plus the 95 % pause as last resort. |
| Proxy | nginx | Caddy | Caddy | **nginx**: Debian security support, `nginx -t`; SSE and redaction tested in T3. |
| Dev-mode guard | static check in apply | `ExecStartPre` curl | `ExecStartPre` curl | **static check** plus a health alarm: nginx must not depend on the hub being up. |
| Secrets at rest | files + `LoadCredential` | systemd-creds | systemd-creds | **files + `LoadCredential`**: restorable onto new hardware in one step; age protects copies. |
| File backups | age bundle | restic | restic + pgBackRest | **age bundle** (one small file, offline-friendly, controller cannot decrypt its own backups) + pgBackRest for history + nightly history summary. |
| Monitoring | uc-health, no node_exporter | node_exporter | node_exporter + textfile | **uc-health** with textfile metrics; node_exporter optional. |
| Time policy | runtime RTC check | runtime | at provision time | **runtime**: an RTC battery can die after installation. |
| Updates | signed offline apt bundle + deb rollback | RAUC A/B + fleet rings | monthly window, ALTER EXTENSION | **Signed bundle + deb rollback + snapshots + post-commit migrations**; RAUC A/B is v2. |
| Remote support | none by default | fleet agent, WireGuard, SSH certificates | none by default | **none by default**; optional SSH user CA now; WireGuard and fleet agent later. |
| Historian and telemetry | archiver stores hub-published samples | hub writes samples | historian ignores telemetry | **historian ignores raw telemetry**; the hub is the only producer of point samples. The E2E test uses a hub shim that implements C2/C3 as the reference. |

## Appendix B. Rejected alternatives

| Alternative | Why rejected |
|---|---|
| SD-card boot | wear and silent corruption |
| NVMe in a USB enclosure | peak power over the 1.2 A budget, bridge variety |
| Pi 5 / CM4 | the user chose the Pi 4; `controller.hardware` keeps the differences local |
| Debian's own raspi images | no rpi-eeprom, vcgencmd, overlays, watchdog/TRIM plumbing |
| cloud-init on production units | FAT re-seeding is an injection path; replaced by `uc-firstboot` |
| Ansible or pull-based config management | needs an admin host and network; drift |
| rpi-image-gen now | A/B slot tooling does not handle `/dev/sdX`; OTA via a vendor cloud; needs native arm64 + podman |
| pi-gen | privileged loop devices, no lock file |
| Hub writes PostgreSQL directly | couples the AI-facing process to schema, TSL and DB lifecycle |
| Historian polls BACnet/SMP itself | doubles device load, duplicates drivers |
| Historian decodes raw MQTT telemetry | duplicates hub profiles, double-counts |
| Hub building model moved into PostgreSQL now | one failure domain for supervision and history |
| VictoriaMetrics, InfluxDB 3 Core, QuestDB, SQLite | numeric-only / no compaction / JVM / write amplification |
| Agent raw SQL in v1 | cost and DoS on a Pi, TSL exposure, injection via log text |
| Grafana on the Pi | RAM and attack surface; the hub UI is the trend viewer |
| systemd-creds host-key encryption | machine-id binding complicates restore |
| Full-disk encryption by default | no supported encrypted root, no AES instructions, unattended recovery |
| Docker on the box | DNAT bypasses the input policy |
| Dynamic-security plugin now | per-device state; `disableClient` does not keep certificate clients out [V] |
| step-ca | devices cannot do ACME/EST/SCEP; issuance is rare |

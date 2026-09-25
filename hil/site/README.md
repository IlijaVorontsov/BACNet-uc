# hil/site: site configuration of the rig's firmware images (D24)

The rig tests each app **as released**: the image is built from the firmware branch's own
sources, plus a short **site-configuration allowlist** that points it at the rig. Nothing else
may differ. SEC-01 diffs every release `.config` against the plain app build and accepts only
the symbols listed here. `hil/host/build.sh` builds every image from these files (docs/HIL.md
§8.3).

## Allowlist

| Image (build.sh name) | Files | Symbols it may change | Why |
|---|---|---|---|
| P1 BACnet release (`bac-rel`) | `f767-app-pool.overlay` + `-DCONFIG_UC_APP_POOL_SIZE=98304` | chosen `uc,app-pool = &dtcm`, `UC_APP_POOL_SIZE` | The default nucleo_f767zi build overflows RAM by 29,564 B (FW-04). This is the workaround documented in the BACnet branch (docs/architecture.md §6.2). It goes away when FW-04 lands: `host/build.sh` and `twister/gen.py` apply it only while the checkout's own `firmware/boards/nucleo_f767zi.overlay` does not choose `uc,app-pool` (BACnet e62a095 and later put the pool into DTCM themselves, 112 KiB, and then build unchanged). |
| P1 BACnet MCUboot (`bac-mcuboot`, It2, OTA-* only) | the branch's own `overlay-mcuboot.conf` (sysbuild) + the workaround | the MCUboot symbols of that file; an OTA candidate adds only a `UC_FW_VERSION` suffix (`-hilota`) | OTA needs MCUboot. Built only when named. |
| P1 MQTT release (`mq-rel`) | `mqtt-site-hil.conf` | `APP_MQTT_BROKER_HOSTNAME="broker.hil.lan"`, `APP_MQTT_TLS_CA_CERT_FILE` = the TEST-ONLY CA `hil/pki/ca.crt` | the rig's broker and CA (tier (a) of B12) |
| P1 MQTT mTLS (`mq-mtls`) | `mqtt-site-hil.conf` + `mqtt-site-hil-mtls.conf` | plus `APP_MQTT_TLS_CLIENT_AUTH=y`, `_CLIENT_CERT_FILE`, `_CLIENT_KEY_FILE` = `hil/pki/dut-client.{crt,key}` | TLS-03 (`hil.mqtt.release.mtls`) |
| P1 MQTT variant (`mq-var`, It2) | `mqtt-site-hil.conf` + `variant-publish120.conf` | plus exactly `APP_MQTT_PUBLISH_INTERVAL_SEC=120` | MQTT-09 needs an idle session longer than the 60 s keepalive. A *variant*: SEC-01 reports it separately from the release artifacts (mark `variant`). |

Instrumented images (`bac-inst`, `mq-inst`) are the release builds plus `-S hil` (and `-S hil-io`
for BACnet), `-DSNIPPET_ROOT=$HIL` and `-DZEPHYR_EXTRA_MODULES=$HIL/lib/hil`. They are not
release artifacts, and a release `.config` must never set `CONFIG_HIL*` (build.sh checks this
too).

## Paths: `@HIL@`

The MQTT app embeds its credential files at build time and resolves a relative path against
its own directory (apps/mqtt_tls/CMakeLists.txt), and a Kconfig fragment cannot name a file
relative to itself. The committed files therefore spell the PKI paths as `@HIL@/hil/pki/...`.
build.sh renders them with the absolute path of the HIL checkout (`sed s|@HIL@|$HIL|`) into
`<out>/site/` and builds from those copies, which also records exactly what each image was
built with. A file used unrendered stops the build with "credential file ... does not exist",
never silently. Twister's alt configs (`hil/twister/gen.py`, It1 W3) render them the same way.

## SIL images (`sil/`, FW-13)

Both apps' own native_sim board configs use NSOS (host sockets). NSOS passes no
SO_BROADCAST, so BACnet Who-Is/I-Am cannot run on it, and NSOS traffic never crosses the rig's
capture interface. For the SIL tier the rig builds both apps for `native_sim/native/64` on the
native **TAP** driver instead:

| File | Used by | What |
|---|---|---|
| `sil/native-sim-tap.conf` | `mq-sil`, `bac-sil` | NSOS off; `ETH_NATIVE_TAP` on interface `zeth`; fixed MAC `02:48:49:4c:00:0a`; DHCPv4 and the connection manager on |
| `sil/mqtt-sil.conf` | `mq-sil` (after `mqtt-site-hil.conf`) | `APP_MQTT_CLIENT_ID="hil-dut"`, the SIL bench's client id |
| (the same two files) | `mq-sil-mtls` (after `mqtt-site-hil.conf` and `mqtt-site-hil-mtls.conf`) | the mTLS image for TLS-03 in SIL |
| (the same two files) | `mq-sil-inst` (only when named) | mq-sil plus `-S hil` and lib/hil: HIL-BOOT/HIL-READY and the `hil` shell without hardware |
| `sil/bench-native-sim.yml` | pytest `--sil --bench` | SIL bench for these images: board `native_sim/native/64`, the TAP MAC (dnsmasq reserves 192.0.2.10 on it), instance 260001, client id `hil-dut` |

The test session does all of the following by itself with `pytest --sil --sil-dut
<app>=<out>/<image>/zephyr/zephyr.exe` (hil/README.md, "SIL against the firmware"). By hand:
`hil/net/up.sh --sil` creates the TAP `zeth` in netns `lan-a` on bridge `br-a`, and with
dnsmasq and a broker running in netns `svc` one image at a time runs inside `lan-a` (all of
them use the same TAP and MAC):

```sh
ip netns exec lan-a <out>/bac-sil/zephyr/zephyr.exe --flash=<tmp>/flash.bin --flash_erase
ip netns exec lan-a <out>/mq-sil/zephyr/zephyr.exe
```

The DUT then gets 192.0.2.10 from dnsmasq like the board does, and the MQTT image resolves
`broker.hil.lan` through the DHCP-provided DNS server (192.0.2.1). The BACnet image also
accepts a static address from the TAP driver's options, `--ipv4-addr=192.0.2.10
--ipv4-nm=255.255.255.0 --ipv4-gw=192.0.2.254`, but it still starts DHCP (device.json default),
so the reservation is the supported path.

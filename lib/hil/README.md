# lib/hil: HIL instrumentation module

A separate Zephyr module (`name: hil`) that instrumented DUT images add, so that nothing changes
in the firmware branches (D26, contract FW-15). Release images never contain it (SEC-01).

```sh
west build -b nucleo_f767zi $FW/firmware -S hil -S hil-io -- \
  -DSNIPPET_ROOT=$HIL -DZEPHYR_EXTRA_MODULES="$FW;$HIL/lib/hil" <release args>
west build -b nucleo_f767zi $MQ/apps/mqtt_tls -S hil -- \
  -DSNIPPET_ROOT=$HIL -DZEPHYR_EXTRA_MODULES="$MQ;$HIL/lib/hil" <release args>
```

(`hil/host/build.sh bac-inst mq-inst` does this; see there for why the firmware checkout is an
extra module too.) `snippets/hil/hil.conf` sets exactly `CONFIG_HIL`, `CONFIG_SHELL`,
`CONFIG_HWINFO` and `CONFIG_REBOOT`. `CONFIG_HIL_SHELL` defaults to y.

## Console lines

| Line | When | Marker |
|---|---|---|
| `HIL-BOOT board=<board target> zephyr=<version> reset=0x<hwinfo cause> uid=<hex>` | once, `SYS_INIT(APPLICATION, 99)` (before `main()`); the reset cause is cleared afterwards | m0 toggles |
| `HIL-READY ip=<IPv4> mac=<xx:xx:xx:xx:xx:xx>` | every `NET_EVENT_IPV4_ADDR_ADD`, and once at boot if the default interface already has an address | m0 toggles |

The lines are printed with `printk`. With `CONFIG_LOG_PRINTK` (the default of both apps) they
pass through the log core without a prefix. The host should search for the line anywhere in the
console text rather than anchor it at the start. The MAC is lower case, like `bench.yml`.

## Shell `hil`

Same framing as the stimulus protocol: exactly one `OK k=v ...` or `ERR -<errno> <text>` line,
with the protocol's fixed numbers (-2, -5, -22, -134) whatever the libc (native_sim's libc has
ENOTSUP = 95).

| Command | Reply |
|---|---|
| `hil info` | `OK board= zephyr= reset=0x<boot cause> uid= markers=<n> ip=<IPv4 or -> uptime_ms=` |
| `hil mark <n> <0\|1\|t>` | `OK m=<n> v=<new level>`; `ERR -2` for an unknown marker, `ERR -22` for bad arguments |
| `hil panic` | `OK panic`, then `k_panic()` 50 ms later (RST-04: the image must recover, FW-05) |

Unknown subcommands answer `ERR -134 unknown command`. `wdt-stall` follows in It2.

## Checked without hardware

`hil/host/build.sh mq-sil-inst` builds mqtt_tls for native_sim with `-S hil` and this module.
Run in netns lan-a against dnsmasq (2026-09-25) it printed

```
HIL-BOOT board=native_sim/native/64 zephyr=4.4.2 reset=0x00000008 uid=007f0100
HIL-READY ip=192.0.2.10 mac=02:48:49:4c:00:0a
```

and answered `hil info`, `hil mark` (`ERR -2`, no markers on native_sim), `hil frob`
(`ERR -134`) and `hil panic` (kernel panic, then the 4.4.2 default handler halts). The markers
and the nucleo_f767zi images are not yet run on hardware.

## Markers

The markers are the `hil-marker-gpios` of `/zephyr,user`, set by the `hil` snippet's board
overlay (PG0-PG3 on nucleo_f767zi). Boards without them (for example native_sim) get the lines
and the shell without markers (`markers=0`). The code is compiled with `-Werror`.

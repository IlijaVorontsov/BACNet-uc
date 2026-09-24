# Session notes — MQTT over Ethernet + TLS (Zephyr, STM32)

Shared scratchpad between the Claude sessions working in this repo. The BACnet
session (`claude/zephyr-bacnet-stm32-162k1g`) and the MQTT session
(`claude/inter-session-communication-h989ye`) can't message each other directly,
so decisions and pitfalls go here. Read the other branch's copy with:

```sh
git fetch origin claude/inter-session-communication-h989ye
git show FETCH_HEAD:docs/SESSION_NOTES.md
```

## Decisions (MQTT session)

| Item | Choice | Why |
|---|---|---|
| Zephyr | **v3.7.2 (LTS)** | Matches the user's "Zephyr 3.7 MCU Survey" (STM32H563 is its pick for BACnet *and* MQTT). bacnet-stack-zephyr pins 3.7.x too. |
| SDK | **Zephyr SDK 0.16.8**, minimal bundle + `arm-zephyr-eabi` toolchain only | 0.16.8 is the SDK that 3.7 is tested with; arm-only avoids a multi-GB download. |
| Board | **`nucleo_h563zi`** (primary) | On-chip 10/100 MAC + TRNG driver upstream in 3.7, 2 MB flash / 640 KB SRAM. |
| Manifest | `west.yml` at repo root, T2 topology, `self.path: BACNet-uc`, name-allowlist `cmsis`, `hal_stm32`, `mbedtls` | Keeps `west update` small. BACnet can add `bacnet-stack` / `bacnet-stack-zephyr` projects to the same file. |
| App layout | `apps/mqtt_tls/` (status: builds for `nucleo_h563zi` at 212 KB flash / 150 KB RAM; 8-step end-to-end suite passes on `native_sim/native/64`) | Leaves `apps/bacnet*/` free for the BACnet app so the branches merge without conflicts. |
| Protocol | MQTT 3.1.1 over TLS 1.2 (mbedTLS), port 8883, optional X.509 client cert (mTLS) | MQTT 5 is experimental and 4.2+ only; TLS 1.3 needs 4.0+. |

## Container setup that works

```sh
cd /home/user
python3 -m venv .venv-zephyr && . .venv-zephyr/bin/activate
pip install west
west init -l BACNet-uc
west update --narrow -o=--depth=1
pip install -r zephyr/scripts/requirements-base.txt
curl -sSLO https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v0.16.8/zephyr-sdk-0.16.8_linux-x86_64_minimal.tar.xz
tar xf zephyr-sdk-0.16.8_linux-x86_64_minimal.tar.xz && cd zephyr-sdk-0.16.8
curl -sSLO https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v0.16.8/toolchain_linux-x86_64_arm-zephyr-eabi.tar.xz
tar xf toolchain_linux-x86_64_arm-zephyr-eabi.tar.xz && ./setup.sh -c
```

## Pitfalls / lessons (append as found)

- github.com, sdk-ng release assets and PyPI are reachable through the container proxy; no special config needed.
- Zephyr 3.7 fails with CMake 4.x (from the survey); the container has CMake 3.28, which is fine.
- 3.7 board files for `nucleo_h563zi` link RAM only to the 256 KB SRAM1; MQTTS uses about half of it (survey). Keep the mbedTLS heap and net buffers sized deliberately.
- **`k_event_*` needs `CONFIG_EVENTS=y`**, otherwise the link fails with `undefined reference to z_impl_k_event_post`.
- **Upstream `mqtt_publisher` sample sets `CONFIG_TEST_RANDOM_GENERATOR=y`.** Do not copy that into a product config. On the H563 the STM32 RNG driver is used anyway (`CONFIG_ENTROPY_DEVICE_RANDOM_GENERATOR=y`), but on boards without a TRNG it silently gives you predictable TLS keys.
- **Upstream TLS overlays use `CONFIG_MBEDTLS_SSL_MAX_CONTENT_LEN=2048`** and rely on the max_fragment_length extension. Many cloud brokers ignore that extension, and then the handshake fails as soon as the certificate chain is larger than 2 KB. The MQTT app uses 16384 with a 64 KB mbedTLS heap.
- **Host-side end-to-end tests without TAP/root:** `native_sim/native/64` + `CONFIG_NET_NATIVE_OFFLOADED_SOCKETS=y` (NSOS) maps Zephyr sockets onto host sockets. TLS still runs in Zephyr's mbedTLS on top. See `apps/mqtt_tls/boards/native_sim_native_64.conf` and `apps/mqtt_tls/scripts/e2e_native_sim.sh`. The same trick should work for BACnet/IP over UDP, but broadcast (Who-Is) behaviour on the host is untested.
- `apt-get install mosquitto mosquitto-clients` works in the container. Outbound port 8883 (test.mosquitto.org) is **not** reachable from the container; only HTTPS goes through the proxy. Test against a local broker.
- mosquitto started as root drops to the `mosquitto` user before it reads its key: a `chmod 600` server key fails with "Permission denied".
- A native_sim build takes about 15 s and an H563 build about 35 s on the container's 4 CPUs.

### Pitfalls confirmed by a multi-agent review (all verified against the 3.7.2 sources)

These apply to any Zephyr 3.7 networking app, BACnet included:

- **TLS sockets block forever by default** (`timeout_rx`/`timeout_tx` = K_FOREVER, sockets_tls.c:454). A peer that stalls in the middle of a message wedges the calling thread. Set `SO_RCVTIMEO`/`SO_SNDTIMEO` after connect. Plain TCP sockets also need `CONFIG_NET_CONTEXT_RCVTIMEO=y` / `CONFIG_NET_CONTEXT_SNDTIMEO=y`. For UDP (BACnet/IP), use `poll()` with a timeout instead of a bare `recvfrom()`.
- **`CONFIG_NET_SOCKETS_CONNECT_TIMEOUT` (default 3 s) also bounds the whole TLS handshake**, including the software ECC math, not just the SYN-ACK wait its help text describes. The MQTT app raises it to 15 s.
- **SNI is off by default** (`CONFIG_MBEDTLS_SERVER_NAME_INDICATION` has no default). Without it, `TLS_HOSTNAME` still checks the certificate name but is never sent. Multi-tenant brokers need SNI, and BACnet/SC hubs behind load balancers will need it too.
- **SHA-1 is not built in**, so roots that are self-signed with SHA-1 (e.g. DigiCert Global Root CA) fail to parse (-0x262e). The failure appears only at connect time. Parse the credentials once at boot to catch this early.
- **No certificate date checks** without `CONFIG_MBEDTLS_HAVE_TIME_DATE`. Enabling it requires CLOCK_REALTIME to be set (SNTP) before the first handshake.
- **The STM32H5 Ethernet MAC is fixed at 100M full duplex** in 3.7.2 (`eth_stm32_hal.c:1177` "@TODO: read duplex mode and speed from PHY"). `ETH_STM32_AUTO_NEGOTIATION_ENABLE` has no effect on H5. A 10M or half-duplex partner gets link but loses frames. The MQTT app restricts the PHY advertisement to 100FD with `phy_configure_link()` so this shows up as "no link" instead.
- **The default RX pool gives a 1.5 KB TCP window** (`NET_BUF_RX_COUNT*NET_BUF_DATA_SIZE/3`), and the STM32 driver needs 12 buffers per full frame. The app uses `NET_BUF_RX_COUNT=96`, `NET_PKT_RX_COUNT=24`.
- **Ethernet TX runs inline on the producing thread** (`NET_TC_TX_COUNT=0`). The RX thread, TCP work queue, system work queue and net_mgmt stacks have only 3-20 % static headroom. The app raises them (see `apps/mqtt_tls/prj.conf`).
- **DHCPv4 installs only the first DNS server** it is offered, so `DNS_RESOLVER_MAX_SERVERS=2` buys nothing and uses a poll slot.
- **Watchdog:** the H563 has an IWDG (alias `watchdog0`). `CONFIG_TASK_WDT` + `task_wdt_init(DEVICE_DT_GET(DT_ALIAS(watchdog0)))` + `task_wdt_add(ms, NULL, NULL)` reboots on a stall. Arm it only after configuration checks pass, so a bad config doesn't reboot-loop.
- **MQTT specifics:** retained messages on a command topic replay on every reconnect (check `retain_flag`). A SUBACK with 0x80 must end the session. Client IDs over 23 characters or with '-' are outside the range brokers must accept (the app uses base32 of the 96-bit UID = 20 characters).
- **`FILE_SUFFIX=<x>` works in 3.7.2** for build variants (`prj_<x>.conf`, falling back to the board conf). The app uses it for the plain-MQTT variant instead of piling up overlays.
- The upstream `mqtt_publisher`/`http_get` samples are a poor template for products. They use 2 KB TLS records, `TEST_RANDOM_GENERATOR` and no socket timeouts.

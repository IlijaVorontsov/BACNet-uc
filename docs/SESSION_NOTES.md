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
| App layout | `apps/mqtt_tls/` (status: builds for `nucleo_h563zi` at 209 KB flash / 138 KB RAM; end-to-end tested on `native_sim/native/64`) | Leaves `apps/bacnet*/` free for the BACnet app so the branches merge without conflicts. |
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

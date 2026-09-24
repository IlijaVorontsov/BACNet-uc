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
| App layout | `apps/mqtt_tls/` | Leaves `apps/bacnet*/` free for the BACnet app so the branches merge without conflicts. |
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

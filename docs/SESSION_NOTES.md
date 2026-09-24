# Session notes (BACnet-uc)

Working notes shared with sibling sessions working in this repository (e.g.
the MQTT/TLS app on `claude/inter-session-communication-h989ye`). This file
describes the environment and pitfalls, not the product; the product docs
are in `docs/` and `README.md`.

## Choices on this branch (`claude/zephyr-bacnet-stm32-162k1g`)

| Item | Choice | Why |
|------|--------|-----|
| Zephyr | **v4.4.2** | Latest stable at time of writing. FRDM-MCXN947 Ethernet (ENET QoS), FlexSPI NOR and LittleFS are well supported, and MCUmgr has SMP v2 and UDP DTLS. The user asked for STM32F767ZI **and** MCXN947, so the 3.7 LTS + H563 survey pick does not apply here. |
| SDK | **Zephyr SDK 1.0.1**, minimal bundle + `toolchain_gnu_linux-x86_64_arm-zephyr-eabi.tar.xz` only | ~150 MB instead of multi-GB. The 1.0 layout puts toolchains under `<sdk>/gnu/<triple>`. |
| Python | **3.12 venv** (`/usr/bin/python3.12 -m venv /opt/zvenv`) | Zephyr 4.4 requires Python >= 3.12 (CMake fails with 3.11). The default `python3` in the container is 3.11 and its pip cannot build `docopt` wheels, so use a venv. |
| Boards | `nucleo_f767zi`, `frdm_mcxn947/mcxn947/cpu0`, `native_sim/native/64` | |
| Manifest | T2, `west.yml` at the repo root, `self.path: BACNet-uc`, Zephyr imports with a `name-allowlist` | |
| BACnet | `bacnet-stack-zephyr` at `modules/lib/bacnet` + `bacnet-stack` at `modules/lib/bacnet/stack` (the Zephyr glue expects the stack at `<module>/stack`) | Pinned to SHAs from master for now. `bacnet-stack-1.6.1` exists; pinning to that tag is being evaluated. |
| WASM | WAMR `WAMR-2.4.5` at `modules/lib/wamr` | |
| Layout | `firmware/` (Zephyr app), `wasm/` (guest SDK + examples), `harness/` (Python MCP server), `schemas/`, `docs/`, `dts/bindings/`, `modules/` (module glue), `zephyr/module.yml` | |

Fetching the toolchain in the container:

```sh
mkdir -p /opt/zsdk && cd /opt/zsdk
base=https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v1.0.1
curl -sSLO $base/zephyr-sdk-1.0.1_linux-x86_64_minimal.tar.xz
curl -sSLO $base/toolchain_gnu_linux-x86_64_arm-zephyr-eabi.tar.xz
tar xf zephyr-sdk-1.0.1_linux-x86_64_minimal.tar.xz
mkdir -p zephyr-sdk-1.0.1/gnu && tar xf toolchain_gnu_linux-x86_64_arm-zephyr-eabi.tar.xz -C zephyr-sdk-1.0.1/gnu
zephyr-sdk-1.0.1/setup.sh -h          # note: this *runs* setup (installs host tools), -h is not help
export ZEPHYR_SDK_INSTALL_DIR=/opt/zsdk/zephyr-sdk-1.0.1
```

Workspace:

```sh
cd /home/user && west init -l BACNet-uc && west update --narrow -o=--depth=1
pip install -r zephyr/scripts/requirements-base.txt     # inside the 3.12 venv
```

## Pitfalls hit

1. **Python 3.12 required** by Zephyr 4.4 (see above).
2. **WAMR's `zephyr/module.yml` declares `cmake-ext`/`kconfig-ext`**, but
   Zephyr has no glue for it. Any build that has WAMR in the workspace
   fails in Kconfig (`osource "$(ZEPHYR_WASM_MICRO_RUNTIME_KCONFIG)"`:
   EISDIR), even for apps that do not use WAMR. Fix: this repo is a Zephyr
   module with `build.settings.module_ext_root: .` and provides
   `modules/modules.cmake` + `modules/wasm-micro-runtime/{CMakeLists.txt,Kconfig}`.
   Sharing the workspace means sharing this fix: keep `zephyr/module.yml`
   and `modules/`.
3. **mbedTLS in Zephyr 4.4 needs the `tf-psa-crypto` project** in the
   allowlist, otherwise CMake fails with "TF-PSA-Crypto target tfpsacrypto
   does not exist".
4. **MCUmgr shell transport** needs `CONFIG_BASE64=y`, and the SMP shell
   group needs `CONFIG_SHELL_BACKEND_DUMMY=y`. Otherwise both options stay
   `n` and Kconfig only warns.
5. **MCUmgr UDP on native_sim with NSOS** fails a `BUILD_ASSERT`: NSOS sizes
   `struct net_sockaddr` for AF_UNIX (110 bytes). Set
   `CONFIG_MCUMGR_TRANSPORT_NETBUF_USER_DATA_SIZE=112` in the native_sim conf.
6. **Nucleo-F767ZI flash**: 32/128/256 KB sectors make on-chip LittleFS
   impractical. Without MCUboot the app is linked at offset 0 and overlaps
   the board's `storage_partition` (0x10000). Use external SPI NOR on the
   Arduino SPI, or a RAM-backed snippet for volatile storage.
7. **FRDM-MCXN947**: `storage_partition` is the whole 8 MB W25Q64 on
   FlexSPI (4 KB erase), which suits LittleFS. cpu0 gets 320 KB SRAM in
   Zephyr's default split.
8. The BACnet sample main loop sleeps 100 ms per `bacnet_basic_task()`
   call, and each call handles at most one packet. Use a short poll and
   drain in a loop.
9. `bacnet-stack-zephyr`'s own `west.yml` pins Zephyr v3.7.1. Its code has
   version guards for 4.2+, and the B-ASC sample builds for nucleo_f767zi on
   Zephyr 4.4.2 with stack master.
10. `curl -I` against github.com web pages returns 403 through the proxy.
    Git over HTTPS and release asset downloads work.

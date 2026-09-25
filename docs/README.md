# BACnet-uc documentation

BACnet-uc is a Zephyr RTOS firmware and tool set for small BACnet/IP
controllers on the ST NUCLEO-F767ZI and the NXP FRDM-MCXN947 (plus a
`native_sim` build that runs as a Linux process). A node is a BACnet
device with IO points, a LittleFS file system, persistent logs, an MCUmgr/SMP
management interface and a WebAssembly runtime (WAMR) for uploadable control
applications. A Python development harness exposes the nodes to AI agents as an
MCP server, so an agent can configure IO, build and deploy applications and
compose distributed BACnet applications across nodes.

## Reading order

| If you want to ... | Read |
|--------------------|------|
| build and run a node in 15 minutes | [getting-started.md](getting-started.md) |
| understand how the firmware is put together | [architecture.md](architecture.md) |
| choose and wire the hardware | [hardware.md](hardware.md) |
| know what the node offers on the BACnet network | [bacnet.md](bacnet.md) |
| map pins to BACnet objects | [io.md](io.md) |
| write an application | [wasm-runtime.md](wasm-runtime.md), then [`wasm/README.md`](../wasm/README.md) |
| configure a node | [configuration.md](configuration.md) |
| find logs, files and flash partitions | [storage-and-logging.md](storage-and-logging.md) |
| talk to a node from your own tool | [management-protocol.md](management-protocol.md) |
| drive nodes from an AI agent | [harness-mcp.md](harness-mcp.md), [distributed-apps.md](distributed-apps.md) |

## Document index

### Node (firmware) side

| Document | Content |
|----------|---------|
| [architecture.md](architecture.md) | System context, software layers, threads and priorities, data flows, boot sequence, memory budgets, failure handling |
| [hardware.md](hardware.md) | Both boards, IO channel catalogs, storage options (external SPI NOR on the F767), RS-485 add-on for MS/TP, probes, bill of materials |
| [bacnet.md](bacnet.md) | Device profile, BIBBs (implemented and planned), object model and ownership, BACnet/IP datalink, COV, priority arrays, PICS skeleton |
| [io.md](io.md) | Channels and points, the `uc,io-channels` devicetree catalog, `io.json`, scaling and debouncing, scan timing, forcing, porting to a new board |
| [wasm-runtime.md](wasm-runtime.md) | Why WebAssembly, WAMR configuration (interpreter, AOT and the MPU), application lifecycle, host ABI reference, memory model, sandboxing, toolchains, deployment |
| [storage-and-logging.md](storage-and-logging.md) | Flash layouts per board (with and without MCUboot), LittleFS parameters, directory layout, log backends, log retrieval, wear and power loss |
| [configuration.md](configuration.md) | `device.json`, `io.json`, `apps.json` field reference, apply/reload semantics, defaults, `CONFIG_UC_*` Kconfig options |
| [getting-started.md](getting-started.md) | Workspace set-up, builds for all boards, flashing, first contact over SMP, configuration, applications, MCP server |
| [management-protocol.md](management-protocol.md) | **Normative**: SMP transports, standard groups, custom groups 64 `uc_app`, 65 `uc_io`, 66 `uc_node` |

### Development harness and system side

| Document | Content |
|----------|---------|
| [harness-mcp.md](harness-mcp.md) | The MCP server: tools, resources, inventory, transports |
| [distributed-apps.md](distributed-apps.md) | System manifests (`schemas/system.schema.json`), links, placement, planning and acceptance tests |
| [simulation.md](simulation.md) | Running several `native_sim` nodes (host, network namespaces, containers) |
| [security.md](security.md) | Threat model, SMP over DTLS, image signing, application permissions |
| [roadmap.md](roadmap.md) | Planned work |

## Normative sources

The documents describe the implementation; where they disagree with one of the
following files, the file wins.

| Contract | File |
|----------|------|
| WebAssembly guest ABI | [`wasm/sdk/include/bacnet_uc.h`](../wasm/sdk/include/bacnet_uc.h) |
| Management protocol | [management-protocol.md](management-protocol.md) |
| Configuration documents | [`schemas/device.schema.json`](../schemas/device.schema.json), [`schemas/io.schema.json`](../schemas/io.schema.json), [`schemas/apps.schema.json`](../schemas/apps.schema.json) |
| System manifest | [`schemas/system.schema.json`](../schemas/system.schema.json) |
| IO catalog binding | [`dts/bindings/uc,io-channels.yaml`](../dts/bindings/uc,io-channels.yaml) |
| Firmware module APIs | [`firmware/include/uc/`](../firmware/include/uc/) |
| Build options | [`firmware/Kconfig`](../firmware/Kconfig), [`firmware/prj.conf`](../firmware/prj.conf), `firmware/boards/*.conf` |
| Link application parameters | [`wasm/examples/uc-link/README.md`](../wasm/examples/uc-link/README.md) |

## Versions

| Component | Version | Location in the west workspace |
|-----------|---------|--------------------------------|
| Zephyr | v4.4.2 | `zephyr/` |
| Zephyr SDK | 1.0.1 (GNU `arm-zephyr-eabi` toolchain) | `$ZEPHYR_SDK_INSTALL_DIR` |
| bacnet-stack-zephyr (Zephyr glue) | pinned SHA, see [`west.yml`](../west.yml) | `modules/lib/bacnet` |
| bacnet-stack | pinned SHA, see [`west.yml`](../west.yml) | `modules/lib/bacnet/stack` |
| WAMR | WAMR-2.4.5 | `modules/lib/wamr` |
| Firmware | `CONFIG_UC_FW_VERSION` (0.1.0) | `BACNet-uc/firmware` |
| WebAssembly host API | `UC_API_VERSION` 1.0 | `bacnet_uc.h` |

## Conventions in these documents

- "Implemented" means present in the sources of this repository. Anything
  else is marked **Planned**. Build status per board: see
  [architecture.md](architecture.md#62-measured-usage).
- Board names are Zephyr board targets: `nucleo_f767zi`,
  `frdm_mcxn947/mcxn947/cpu0`, `native_sim/native/64`.
- Paths such as `/lfs/cfg/device.json` are paths on the node's file system;
  paths such as `firmware/prj.conf` are relative to the repository root.
- Sizes: KiB = 1024 bytes. Flash and RAM figures are from `west build` output
  of this repository unless stated otherwise.

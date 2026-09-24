# Vendored interface files

Copied unchanged from the BACnet firmware branch
(`claude/zephyr-bacnet-stm32-162k1g`, commit 93fe35b) so the hub can be built
and tested on its own. The firmware branch is the source of truth. When the
branches merge, delete these copies and the ones in
`src/uc_hub/manifest/schemas/` (except `site.schema.json`) and point to the
originals.

| File | Origin |
|---|---|
| `management-protocol.md` | `docs/management-protocol.md` |
| `bacnet_uc.h` | `wasm/sdk/include/bacnet_uc.h` |
| `../src/uc_hub/manifest/schemas/{system,device,io,apps}.schema.json` | `schemas/` |
| `../tests/example_{device,io,apps}.json` | `schemas/examples/` |

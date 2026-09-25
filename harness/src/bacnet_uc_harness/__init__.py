# SPDX-License-Identifier: Apache-2.0
"""BACnet-uc development harness.

Sub-packages:

- ``smp``: MCUmgr/SMP v2 client (UDP and serial console transports) with
  the BACnet-uc custom groups (``uc_app``, ``uc_io``, ``uc_node``).
- ``bacnet``: small dependency-free BACnet/IP client (Who-Is, ReadProperty,
  WriteProperty).
- ``testing``: in-process fake node for tests without hardware.

The remaining modules (manifests, planner, MCP server, CLI) build on these.
"""

from bacnet_uc_harness.errors import BacnetError, HarnessError, HarnessTimeout, SmpError

__version__ = "0.1.0"

__all__ = [
    "BacnetError",
    "HarnessError",
    "HarnessTimeout",
    "SmpError",
    "__version__",
]

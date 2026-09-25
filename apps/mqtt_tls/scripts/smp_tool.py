#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Minimal SMP (MCUmgr) client for the mqtt_tls app, over UDP.

Used by e2e_native_sim.sh and handy on the bench. Needs `pip install smpmgr`
(which brings smpclient).

  smp_tool.py HOST echo TEXT
  smp_tool.py HOST read NAME            e.g. mqtt/broker_host
  smp_tool.py HOST write NAME VALUE     runtime write (not persisted)
  smp_tool.py HOST delete NAME          (refused by the device)
  smp_tool.py HOST save                 persist runtime writes
  smp_tool.py HOST factory-reset        write mqtt/factory_reset
  smp_tool.py HOST reset                reboot the device

Prints the result on stdout; exits non-zero on an SMP error.
"""

import asyncio
import sys

from smpclient import SMPClient
from smpclient.generics import error, success
from smpclient.requests.os_management import EchoWrite, ResetWrite
from smpclient.requests.settings_management import (
    DeleteSetting,
    ReadSetting,
    SaveSettings,
    WriteSetting,
)
from smpclient.transport.udp import SMPUDPTransport


async def run(host: str, cmd: str, args: list[str]) -> int:
    async with SMPClient(SMPUDPTransport(), host, timeout_s=3.0) as client:
        if cmd == "echo":
            req = EchoWrite(d=args[0])
        elif cmd == "read":
            req = ReadSetting(name=args[0])
        elif cmd == "write":
            req = WriteSetting(name=args[0], val=args[1].encode())
        elif cmd == "delete":
            req = DeleteSetting(name=args[0])
        elif cmd == "factory-reset":
            req = WriteSetting(name="mqtt/factory_reset", val=b"1")
        elif cmd == "save":
            req = SaveSettings()
        elif cmd == "reset":
            req = ResetWrite()
        else:
            print(f"unknown command {cmd}", file=sys.stderr)
            return 2

        rsp = await client.request(req)
        if error(rsp):
            print(f"error: {rsp}", file=sys.stderr)
            return 1
        if not success(rsp):
            print(f"unexpected response: {rsp}", file=sys.stderr)
            return 1
        if cmd == "echo":
            print(rsp.r)
        elif cmd == "read":
            print(rsp.val.decode(errors="replace"))
        else:
            print("ok")
        return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    # smpclient always uses the SMP UDP port 1337.
    return asyncio.run(run(sys.argv[1], sys.argv[2], sys.argv[3:]))


if __name__ == "__main__":
    sys.exit(main())

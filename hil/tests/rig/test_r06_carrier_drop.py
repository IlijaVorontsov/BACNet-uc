"""R-06 carrier drop seen by the DUT (rig; hardware).

Catalogue R-06: after the DUT NIC is taken down for 5 s and back up, the DUT sends
DHCPREQUEST or DHCPDISCOVER within 5 s of link-up. This shows it saw carrier loss (otherwise
NET-03 switches to uhubctl on a USB NIC).

Link-up time and frame times are both the host clock (functional deadline).
"""

from __future__ import annotations

import time

import pytest

from hilrig import netns as nsmod
from hilrig.bench import Bench
from hilrig.capture import Capture
from hilrig.netns import Topology

pytestmark = [pytest.mark.rig, pytest.mark.hil_only]

DOWN_S, DEADLINE_S = 5.0, 5.0
DHCP_DISCOVER, DHCP_REQUEST = 1, 3


@pytest.mark.capture("udp port 67 or udp port 68")
def test_r06_carrier_drop_seen_by_the_dut(capture: Capture, netns: Topology, bench: Bench) -> None:
    nic, lan_a = netns.capture_iface, netns.ns("lan-a")
    nsmod.set_link(lan_a, nic, up=False)
    time.sleep(DOWN_S)
    t_up = nsmod.set_link(lan_a, nic, up=True)
    time.sleep(DEADLINE_S + 2.0)
    capture.stop()
    rows = capture.rows(
        f"eth.src == {bench.dut.mac} && dhcp.option.dhcp in {{{DHCP_DISCOVER} {DHCP_REQUEST}}}",
        "dhcp.option.dhcp",
    )
    after = [r for r in rows if r.t >= t_up]
    assert after, (
        f"no DHCPDISCOVER/DHCPREQUEST from {bench.dut.mac} after link-up: the DUT did not see the drop"
    )
    assert after[0].t - t_up <= DEADLINE_S, f"first DHCP message {after[0].t - t_up:.1f} s after link-up"

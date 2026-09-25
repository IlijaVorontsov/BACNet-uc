"""BIP-01 Who-Is / I-Am (release; hardware and SIL).

Catalogue BIP-01: 100/100 Who-Is answered by an I-Am from 192.0.2.10:47808 with the bench
instance, max-APDU, segmentation and vendor id. No _ws.malformed. Latency is informational
until R-05.

Expected I-Am values are the device profile of docs/bacnet.md 1 (BACnet branch):
Max_APDU_Length_Accepted 1476, segmentation not supported, vendor id 260 (bacnet-stack
default). The capture fixture fails the test on any malformed frame from the DUT.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable

import pytest

from hilrig.bacnet import Bacnet
from hilrig.bench import Bench
from hilrig.capture import Capture
from hilrig.netns import HOSTS
from hilrig.release import DutImage

pytestmark = pytest.mark.release

WHO_IS_COUNT = 100
MAX_APDU, SEGMENTATION, VENDOR_ID = "(Unsigned) 1476", "no-segmentation (3)", "(260)"


@pytest.mark.capture("udp port 47808")
@pytest.mark.parametrize("app_image", ["bacnet"], indirect=True)
def test_bip01_who_is_i_am(
    app_image: DutImage,
    bacnet: Bacnet,
    capture: Capture,
    bench: Bench,
    record_property: Callable[[str, object], None],
) -> None:
    inst, dut = bench.dut.bacnet_instance, bench.dut.ip
    for i in range(WHO_IS_COUNT):
        found = bacnet.whois(inst, inst, wait_ms=400)
        assert [(d.instance, d.address) for d in found] == [(inst, (dut, 47808))], f"Who-Is {i}: {found}"
    capture.stop()
    iam = f"bacapp.unconfirmed_service == 0 && ip.src == {dut} && udp.srcport == 47808"
    iam += f" && bacapp.instance_number == {inst}"
    rows = capture.texts(iam)
    assert len(rows) >= WHO_IS_COUNT, f"{len(rows)} I-Am frames for {WHO_IS_COUNT} Who-Is"
    for row in rows:
        assert row.value("Maximum ADPU Length Accepted") == MAX_APDU, row.texts
        assert row.value("Segmentation Supported") == SEGMENTATION, row.texts
        assert (row.value("Vendor ID") or "").endswith(VENDOR_ID), row.texts
    who = [r.t for r in capture.rows(f"bacapp.unconfirmed_service == 8 && ip.src == {HOSTS['svc'].ip}")]
    latencies = [min((r.t - w for r in rows if r.t >= w), default=float("nan")) * 1e3 for w in who]
    valid = [x for x in latencies if x == x]
    if valid:  # informational until the SYNC fit (R-05)
        record_property("iam_latency_ms_median", round(statistics.median(valid), 2))
        record_property("iam_latency_ms_max", round(max(valid), 2))

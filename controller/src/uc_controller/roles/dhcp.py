"""dhcp role (order 35): dnsmasq for the BMS network (DESIGN.md §5.2).

The file /etc/dnsmasq.d/uc-bms.conf exists only while services.dhcp.enabled;
the dnsmasq drop-in (rootfs) has ConditionPathExists= on it, and the unit is
enabled only in that case. Reservations come from the active devices of
devices.yaml that have a MAC and an IP.
"""

from __future__ import annotations

import ipaddress
import logging
import re

from ..roles.base import RenderContext, RenderedFile, Role, Validator

log = logging.getLogger(__name__)

CONF = "/etc/dnsmasq.d/uc-bms.conf"
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _attr(dev, name):
    return dev.get(name) if isinstance(dev, dict) else getattr(dev, name, None)


def reservations(devices, bms_net: str) -> list[dict]:
    """dhcp-host entries: active devices with a valid MAC and an IP inside the BMS network."""
    net = ipaddress.IPv4Network(bms_net)
    out = []
    for dev in devices:
        if _attr(dev, "status") not in (None, "active"):
            continue
        mac, ip = _attr(dev, "mac"), _attr(dev, "ip")
        if not mac or not ip:
            continue
        mac = str(mac).lower()
        try:
            addr = ipaddress.IPv4Address(str(ip))
        except ValueError:
            addr = None
        if not MAC_RE.match(mac) or addr is None or addr not in net:
            log.warning("device %s: reservation %s/%s skipped (invalid or outside %s)",
                        _attr(dev, "id"), mac, ip, bms_net)
            continue
        # the label is a comment line; keep it to printable characters
        label = " ".join(str(x) for x in (_attr(dev, "name"), _attr(dev, "id")) if x)
        label = re.sub(r"[^\x20-\x7e]", "?", label)[:120]
        out.append({"mac": mac, "ip": str(addr), "label": label or mac})
    return sorted(out, key=lambda r: ipaddress.IPv4Address(r["ip"]))


class DhcpRole(Role):
    name = "dhcp"
    order = 35
    units = {"dnsmasq": "restart"}

    def render(self, ctx: RenderContext) -> list[RenderedFile]:
        dhcp = ctx.site.services.dhcp
        if not dhcp.enabled:
            return []   # a file from an earlier apply is removed as stale
        text = ctx.tpl.render("dhcp/uc-bms.conf.j2", site=ctx.site, d=ctx.derived, dhcp=dhcp,
                              reservations=reservations(ctx.devices, ctx.derived.bms_net))
        return [RenderedFile(CONF, text.encode())]

    def validators(self, ctx: RenderContext) -> list[Validator]:
        if not ctx.site.services.dhcp.enabled:
            return []
        return [Validator("dnsmasq --test", ["dnsmasq", "--test", "-C", "{staged}" + CONF], needs="dnsmasq")]

    def enable_units(self, ctx: RenderContext) -> dict[str, bool]:
        return {"dnsmasq": ctx.site.services.dhcp.enabled}


ROLE = DhcpRole()

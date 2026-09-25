"""network role (order 20): systemd-networkd files and /etc/nftables.conf
(DESIGN.md §6).

Interfaces by mode (§5.1, §6.1):

- trunk: eth0 carries VLAN it0 (it.vlan) and VLAN bms0 (bms.vlan), no address;
- dual:  eth0 is renamed it0, the USB NIC with bms.nic_mac is renamed bms0;
- flat:  eth0 is renamed bms0 (lab and HIL); IT access uses the source sets.

The onboard MAC is pinned (``MACAddress=`` in the eth0 .link file) when
``network.mac`` is a MAC, or ``auto`` with ``pinned_mac`` in board.json.
"""

from __future__ import annotations

from ..roles.base import Probe, ProbeResult, RenderContext, RenderedFile, Role, Validator

NETDIR = "/etc/systemd/network"
NFT_CONF = "/etc/nftables.conf"
RESOLV_CONF = "/etc/resolv.conf"


def https_egress_reason(site) -> str:
    """Why tcp 443 out of it0 is open ('' = closed)."""
    reasons = []
    if site.services.updates.online:
        reasons.append("updates.online")
    if site.services.hub.enabled and site.services.hub.llm.provider != "none":
        reasons.append(f"llm {site.services.hub.llm.provider}")
    return ", ".join(reasons)


class NetworkRole(Role):
    name = "network"
    order = 20
    units = {"systemd-networkd": "reload", "nftables": "reload"}

    def render(self, ctx: RenderContext) -> list[RenderedFile]:
        site, d = ctx.site, ctx.derived
        mode = site.network.mode
        files: list[RenderedFile] = []

        def add(path: str, template: str, **extra) -> None:
            text = ctx.tpl.render(template, site=site, d=d, hold=ctx.hold, **extra)
            files.append(RenderedFile(path=path, content=text.encode()))

        # onboard NIC: rename (dual/flat) and/or MAC pin
        rename = {"trunk": None, "dual": d.it_if, "flat": d.bms_if}[mode]
        if rename or d.pinned_mac:
            purpose = []
            if rename:
                purpose.append(f"renamed {rename}")
            if d.pinned_mac:
                purpose.append(f"MAC pinned to {d.pinned_mac}")
            add(f"{NETDIR}/10-uc-eth0.link", "network/10-uc-eth0.link.j2",
                name=rename, purpose=", ".join(purpose))
        if mode == "dual":
            add(f"{NETDIR}/20-uc-bms-nic.link", "network/20-uc-bms-nic.link.j2")
        if mode == "trunk":
            add(f"{NETDIR}/30-uc-it0.netdev", "network/30-uc-it0.netdev.j2")
            add(f"{NETDIR}/30-uc-bms0.netdev", "network/30-uc-bms0.netdev.j2")
            add(f"{NETDIR}/40-uc-eth0.network", "network/40-uc-eth0.network.j2")
        if mode in ("trunk", "dual"):
            add(f"{NETDIR}/50-uc-it0.network", "network/50-uc-it0.network.j2")
            if d.it_dns:
                add(RESOLV_CONF, "network/resolv.conf.j2")
        bms_origin = {"trunk": f"VLAN {d.bms_vlan} on {d.parent}",
                      "dual": f"USB NIC {d.bms_nic_mac}",
                      "flat": f"renamed {d.parent}"}[mode]
        add(f"{NETDIR}/50-uc-bms0.network", "network/50-uc-bms0.network.j2", bms_origin=bms_origin)

        sources = {k: list(getattr(site.network.sources, k)) for k in ("admin", "ui", "backup", "bbmd")}
        text = ctx.tpl.render("network/nftables.conf.j2", site=site, d=d, hold=ctx.hold,
                              flat=mode == "flat", sources=sources,
                              https_egress=https_egress_reason(site))
        files.append(RenderedFile(path=NFT_CONF, content=text.encode(), mode=0o755))
        return files

    def validators(self, ctx: RenderContext) -> list[Validator]:
        return [Validator("nft -c", ["nft", "-c", "-f", "{staged}" + NFT_CONF], needs="nft")]

    def unit_actions(self, changed: list[str]) -> dict[str, str]:
        actions = {}
        if any(p.startswith(NETDIR + "/") for p in changed):
            actions["systemd-networkd"] = "reload"
        if NFT_CONF in changed:
            actions["nftables"] = "reload"
        return actions

    def enable_units(self, ctx: RenderContext) -> dict[str, bool]:
        return {"systemd-networkd": True, "nftables": True}

    def probes(self, ctx: RenderContext) -> list[Probe]:
        probes = [Probe("nft table inet uc", _probe_nft_table)]
        if not ctx.facts.container:
            probes.append(Probe("bms0 address", _probe_bms_address))
        return probes


def _probe_nft_table(ctx: RenderContext, runner) -> ProbeResult:
    res = runner.run(["nft", "list", "table", "inet", "uc"], check=False)
    if res.returncode != 0:
        return ProbeResult(False, (res.stderr or "nft list failed").strip()[-200:])
    want = "chain in_bms"
    return ProbeResult(want in res.stdout, "table inet uc loaded" if want in res.stdout else "chain in_bms missing")


def _probe_bms_address(ctx: RenderContext, runner) -> ProbeResult:
    d = ctx.derived
    res = runner.run(["ip", "-o", "-4", "addr", "show", "dev", d.bms_if], check=False)
    if res.returncode != 0:
        return ProbeResult(False, f"{d.bms_if}: {(res.stderr or '').strip()}")
    if f" {d.bms_cidr} " in res.stdout or f"inet {d.bms_cidr}" in res.stdout:
        return ProbeResult(True, f"{d.bms_if} has {d.bms_cidr}")
    return ProbeResult(False, f"{d.bms_if} lacks {d.bms_cidr}")


ROLE = NetworkRole()

#!/bin/bash
# hil/net/up.sh - create the HIL network namespaces (design v2, decision B7). Idempotent.
#
#   subnet A 192.0.2.0/24, bridge br-a in netns lan-a
#     svc   svc0  192.0.2.1    dnsmasq, mosquitto, chrony, test clients
#     sim1  sim10 192.0.2.11   simulated BACnet devices
#     sim2  sim20 192.0.2.12
#     rtr   rtra  192.0.2.254  plain IP router (ip_forward=1), no broadcast forwarding
#     DUT         192.0.2.10   real NIC (--dut-iface), TAP zeth (--sil) or netns dut (--standin)
#   subnet B 198.51.100.0/24, bridge br-b in netns lan-b
#     rtr   rtrb  198.51.100.254
#     fd    fd0   198.51.100.10  foreign-device client
#     bbmdb bb0   198.51.100.2   peer BBMD
#
# Usage: up.sh [--prefix P] [--dut-iface NIC | --sil | --standin]
#   --prefix P       prefix every namespace name (P = 1-6 of [a-z0-9] then '-'), so tests
#                    can build private copies next to the rig topology
#   --dut-iface NIC  move this physical NIC into lan-a, turn its offloads off, add it to br-a
#   --sil            create TAP 'zeth' in lan-a on br-a; native_sim then runs inside lan-a
#   --standin        create netns 'dut' (dut0 192.0.2.10) on br-a for a bacserv stand-in DUT
#
# Installed root-owned as /usr/local/sbin/hil-net-up by hil/host/install-net-wrappers.sh.
# sudoers lets the hil group run it with any arguments, so every argument is validated here
# and nothing is read from the caller's environment or work tree.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin

die() { echo "hil-net-up: $*" >&2; exit 2; }

prefix="" dut_iface="" sil=0 standin=0
while (($#)); do
	case $1 in
	--prefix) (($# >= 2)) || die "--prefix needs a value"; prefix=$2; shift 2 ;;
	--dut-iface) (($# >= 2)) || die "--dut-iface needs a value"; dut_iface=$2; shift 2 ;;
	--sil) sil=1; shift ;;
	--standin) standin=1; shift ;;
	-h | --help) sed -n '2,26p' "$0"; exit 0 ;;
	*) die "unknown argument '$1' (see --help)" ;;
	esac
done

[[ $prefix =~ ^([a-z0-9]{1,6}-)?$ ]] || die "invalid --prefix '$prefix' (1-6 of [a-z0-9], then '-')"
[[ -z $dut_iface || $dut_iface =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}$ ]] || die "invalid interface name '$dut_iface'"
attachments=$((sil + standin))
[[ -z $dut_iface ]] || attachments=$((attachments + 1))
((attachments <= 1)) || die "--dut-iface, --sil and --standin are exclusive: each one puts a DUT at 192.0.2.10"
(( EUID == 0 )) || die "must run as root (sudo hil-net-up ...)"

LAN_A=${prefix}lan-a LAN_B=${prefix}lan-b RTR=${prefix}rtr

ensure_ns() {
	[[ -e /run/netns/$1 ]] || ip netns add "$1"
	ip -n "$1" link set lo up
}

has_link() { ip -n "$1" link show dev "$2" >/dev/null 2>&1; }

ensure_bridge() { # netns bridge
	has_link "$1" "$2" || ip -n "$1" link add "$2" type bridge
	ip -n "$1" link set "$2" up
}

# attach netns ifname bridge-netns bridge cidr [gateway]: veth ifname in netns, peer p-ifname
# enslaved to the bridge. Both ends are created directly inside their namespaces.
attach() {
	local ns=$1 ifc=$2 brns=$3 br=$4 cidr=$5 gw=${6:-}
	has_link "$ns" "$ifc" || ip link add "$ifc" netns "$ns" type veth peer name "p-$ifc" netns "$brns"
	ip -n "$brns" link set "p-$ifc" master "$br" up
	ip -n "$ns" addr replace "$cidr" dev "$ifc"
	ip -n "$ns" link set "$ifc" up
	[[ -z $gw ]] || ip -n "$ns" route replace default via "$gw"
}

# Checks for the real NIC happen before anything is created, so a refusal leaves no state.
if [[ -n $dut_iface ]] && ! has_link "$LAN_A" "$dut_iface"; then
	ip link show dev "$dut_iface" >/dev/null 2>&1 || die "no interface '$dut_iface' in the root namespace"
	[[ -e /sys/class/net/$dut_iface/device ]] || die "'$dut_iface' is not a physical NIC"
	if ip -4 route show default | grep -qw "dev $dut_iface"; then
		die "'$dut_iface' carries the default route; refusing to move the management NIC"
	fi
fi
[[ -z $dut_iface ]] || command -v ethtool >/dev/null || die "--dut-iface needs ethtool (apt install ethtool)"

for n in lan-a svc sim1 sim2 rtr lan-b fd bbmdb; do ensure_ns "$prefix$n"; done
ensure_bridge "$LAN_A" br-a
ensure_bridge "$LAN_B" br-b

attach "${prefix}svc" svc0 "$LAN_A" br-a 192.0.2.1/24 192.0.2.254
attach "${prefix}sim1" sim10 "$LAN_A" br-a 192.0.2.11/24 192.0.2.254
attach "${prefix}sim2" sim20 "$LAN_A" br-a 192.0.2.12/24 192.0.2.254
attach "$RTR" rtra "$LAN_A" br-a 192.0.2.254/24
attach "$RTR" rtrb "$LAN_B" br-b 198.51.100.254/24
attach "${prefix}fd" fd0 "$LAN_B" br-b 198.51.100.10/24 198.51.100.254
attach "${prefix}bbmdb" bb0 "$LAN_B" br-b 198.51.100.2/24 198.51.100.254
ip netns exec "$RTR" sysctl -qw net.ipv4.ip_forward=1

if [[ -n $dut_iface ]]; then
	if ! has_link "$LAN_A" "$dut_iface"; then
		ip link set dev "$dut_iface" down
		ip addr flush dev "$dut_iface"
		ip link set dev "$dut_iface" netns "$LAN_A"
	fi
	# The capture on this NIC must show what is on the wire, not coalesced or unchecksummed
	# host buffers. Drivers refuse some features; that is reported, not fatal.
	for feature in rx tx tso gso gro lro; do
		ip netns exec "$LAN_A" ethtool -K "$dut_iface" "$feature" off 2>/dev/null ||
			echo "hil-net-up: $dut_iface: could not turn '$feature' off" >&2
	done
	ip -n "$LAN_A" link set dev "$dut_iface" master br-a up
fi

if ((sil)); then
	has_link "$LAN_A" zeth || ip -n "$LAN_A" tuntap add dev zeth mode tap
	ip -n "$LAN_A" link set dev zeth master br-a up
fi

if ((standin)); then
	ensure_ns "${prefix}dut"
	attach "${prefix}dut" dut0 "$LAN_A" br-a 192.0.2.10/24 192.0.2.254
fi

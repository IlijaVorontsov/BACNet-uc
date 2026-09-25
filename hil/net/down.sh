#!/bin/bash
# hil/net/down.sh - remove the namespaces created by up.sh. Idempotent.
#
# Usage: down.sh [--prefix P]
#
# Processes still running inside the namespaces are terminated (SIGTERM, then SIGKILL after
# 2 s). A physical DUT NIC is moved back to the root namespace explicitly before lan-a is
# deleted: the kernel would only return it once the last socket in lan-a is closed.
#
# Installed root-owned as /usr/local/sbin/hil-net-down by hil/host/install-net-wrappers.sh;
# arguments are validated here because sudoers allows any.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin

die() { echo "hil-net-down: $*" >&2; exit 2; }

prefix=""
while (($#)); do
	case $1 in
	--prefix) (($# >= 2)) || die "--prefix needs a value"; prefix=$2; shift 2 ;;
	-h | --help) sed -n '2,12p' "$0"; exit 0 ;;
	*) die "unknown argument '$1' (see --help)" ;;
	esac
done
[[ $prefix =~ ^([a-z0-9]{1,6}-)?$ ]] || die "invalid --prefix '$prefix' (1-6 of [a-z0-9], then '-')"
(( EUID == 0 )) || die "must run as root (sudo hil-net-down ...)"

stop_processes() {
	local pids
	pids=$(ip netns pids "$1")
	[[ -n $pids ]] || return 0
	# shellcheck disable=SC2086
	kill $pids 2>/dev/null || true
	for _ in $(seq 20); do
		[[ -n $(ip netns pids "$1") ]] || return 0
		sleep 0.1
	done
	pids=$(ip netns pids "$1")
	# shellcheck disable=SC2086
	[[ -z $pids ]] || kill -9 $pids 2>/dev/null || true
}

LAN_A=${prefix}lan-a
if [[ -e /run/netns/$LAN_A ]]; then
	for dev in $(ip netns exec "$LAN_A" ls /sys/class/net); do
		if ip netns exec "$LAN_A" test -e "/sys/class/net/$dev/device"; then
			ip -n "$LAN_A" link set dev "$dev" netns 1
		fi
	done
fi

for n in dut fd bbmdb sim1 sim2 svc rtr lan-b lan-a; do
	ns=$prefix$n
	[[ -e /run/netns/$ns ]] || continue
	stop_processes "$ns"
	ip netns del "$ns"
done

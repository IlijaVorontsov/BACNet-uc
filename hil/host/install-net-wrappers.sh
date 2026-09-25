#!/bin/bash
# hil/host/install-net-wrappers.sh - install the rig's two privileged entry points.
#
# Copies hil/net/up.sh and down.sh, root-owned, to /usr/local/sbin/hil-net-up and
# hil-net-down, and lets members of group $HIL_GROUP (default: hil) run exactly those two
# through sudo without a password. Nothing else is allowed: a rule for 'ip netns exec' is
# a root shell, and a rule for scripts in a work tree gives root to whoever edits the tree.
# Re-run after changing hil/net/*.sh.
#
# Usage: sudo hil/host/install-net-wrappers.sh
# Environment: HIL_GROUP (hil), DESTDIR (staging root, e.g. for packaging and tests).
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin

group=${HIL_GROUP:-hil}
dest=${DESTDIR:-}
src=$(cd "$(dirname "$0")/../net" && pwd)

[[ $group =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "invalid HIL_GROUP '$group'" >&2; exit 2; }
if [[ -z $dest ]]; then
	((EUID == 0)) || { echo "run as root: sudo $0" >&2; exit 1; }
	owner=(-o root -g root)
else
	owner=()
fi

install -d "$dest/usr/local/sbin" "$dest/etc/sudoers.d"
install "${owner[@]}" -m 0755 "$src/up.sh" "$dest/usr/local/sbin/hil-net-up"
install "${owner[@]}" -m 0755 "$src/down.sh" "$dest/usr/local/sbin/hil-net-down"

rule=$(mktemp)
trap 'rm -f "$rule"' EXIT
cat >"$rule" <<EOF
# Installed by hil/host/install-net-wrappers.sh. The two root-owned wrappers validate their
# arguments; never add 'ip netns exec' or work-tree scripts here.
%$group ALL=(root) NOPASSWD: /usr/local/sbin/hil-net-up, /usr/local/sbin/hil-net-up *, \\
    /usr/local/sbin/hil-net-down, /usr/local/sbin/hil-net-down *
EOF
visudo -cq -f "$rule"
install "${owner[@]}" -m 0440 "$rule" "$dest/etc/sudoers.d/hil-net"
echo "installed $dest/usr/local/sbin/hil-net-{up,down} and $dest/etc/sudoers.d/hil-net (group $group)"

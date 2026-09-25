#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Run the harness end-to-end tests in private network and mount namespaces:
# the native_sim nodes, the simulation bridge bnuc0 and the named network
# namespaces (a private tmpfs on /run/netns) are invisible to the rest of the
# machine, so the tests neither collide with other simulations nor leave
# anything behind. Needs root (unshare, mount).
#
#   sudo BACNET_UC_FIRMWARE=/tmp/build-native/zephyr/zephyr.exe \
#       harness/tests/e2e/run-isolated.sh [pytest arguments]
#
# PYTHON selects the interpreter (default: python3); extra arguments go to
# pytest (default: -m e2e tests/e2e, run from the harness directory).
set -eu

here=$(cd "$(dirname "$0")/../.." && pwd)
PYTHON=${PYTHON:-python3}
export PYTHON

if [ "$#" -eq 0 ]; then
	set -- -m e2e tests/e2e
fi

# the script runs in the new namespaces; $0 and $@ expand there
# shellcheck disable=SC2016
exec unshare --net --mount sh -eu -c '
	mount --make-rprivate /
	mkdir -p /run/netns
	mount -t tmpfs -o mode=0755 bnuc-netns /run/netns
	if command -v ip >/dev/null 2>&1; then
		ip link set lo up
	else
		"$PYTHON" -c "from pyroute2 import IPRoute
with IPRoute() as ipr:
    ipr.link(\"set\", index=ipr.link_lookup(ifname=\"lo\")[0], state=\"up\")"
	fi
	cd "$0"
	exec "$PYTHON" -m pytest "$@"
' "$here" "$@"

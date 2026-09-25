#!/bin/bash
# hil/host/isolated.sh - run a command (a pytest --sil session) in a private network and mount
# namespace, so its rig topology cannot collide with another run on the same host.
#
# Usage: sudo hil/host/isolated.sh [-d] COMMAND [ARG ...]
#   -d  also start a dockerd of its own inside, for the eclipse-mosquitto:2.1.2-alpine broker
#       (TLS key log). A host dockerd cannot serve an isolated run: it resolves /run/netns/svc
#       in the host's mount namespace, not in this one.
#
# Why: the rig's namespaces have fixed names (lan-a, svc, ..., and hu-/hn- for the module
# tests). Two runs on one host would tear down each other's topology. Inside, /run/netns is a
# private tmpfs and the run gets a fresh network namespace with only lo.
#
# The private dockerd keeps its sockets, pid file and exec root on a private tmpfs at /srv,
# with --bridge none and no iptables changes (the broker container joins netns svc through
# nsenter, see hilrig.services.Mosquitto). Its image store is $DOCKER_DATA_ROOT (default
# /var/lib/docker): pull the image there once, and do not share that directory with a dockerd
# that is running at the same time. The dockerd is stopped when the command ends, when it
# fails to come up and when the run is interrupted; its log is $DOCKER_LOG (default
# /tmp/hil-isolated-dockerd.log). Exit status: that of COMMAND.
set -euo pipefail

docker=0
if [[ ${1:-} == -d ]]; then
	docker=1
	shift
fi
(($#)) || { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0" >&2; exit 2; }
((EUID == 0)) || { echo "isolated.sh: needs root (unshare, setns)" >&2; exit 2; }

export HIL_ISOLATED_DOCKER=$docker
export DOCKER_DATA_ROOT=${DOCKER_DATA_ROOT:-/var/lib/docker}
export DOCKER_LOG=${DOCKER_LOG:-/tmp/hil-isolated-dockerd.log}
# shellcheck disable=SC2016  # the inner script expands its variables inside the namespace
exec unshare --mount --net --propagation private bash -c '
	set -euo pipefail
	mkdir -p /run/netns && mount -t tmpfs tmpfs /run/netns && ip link set lo up
	# The private dockerd must end with this shell however it ends (it did not come up, the
	# command failed, Ctrl-C or SIGTERM): otherwise it keeps running, with its containers, in a
	# namespace nobody can reach any more. INT and TERM become exits, so the EXIT trap runs.
	dockerd_pid=""
	stop_dockerd() {
		[[ -n $dockerd_pid ]] || return 0
		kill "$dockerd_pid" 2>/dev/null || true
		wait "$dockerd_pid" 2>/dev/null || true
	}
	trap stop_dockerd EXIT
	trap "exit 130" INT
	trap "exit 143" TERM
	if ((HIL_ISOLATED_DOCKER)); then
		mount -t tmpfs tmpfs /srv
		dockerd --data-root "$DOCKER_DATA_ROOT" --exec-root /srv/exec --pidfile /srv/docker.pid \
			--host unix:///srv/docker.sock --bridge none --iptables=false --ip6tables=false \
			>"$DOCKER_LOG" 2>&1 &
		dockerd_pid=$!
		export DOCKER_HOST=unix:///srv/docker.sock
		for _ in $(seq 120); do docker info >/dev/null 2>&1 && break; sleep 0.5; done
		docker info >/dev/null || { echo "isolated.sh: dockerd did not start ($DOCKER_LOG)" >&2; exit 2; }
	fi
	rc=0
	"$@" || rc=$?
	exit "$rc"' isolated "$@"

#!/bin/bash
# hil/host/install-runner.sh - set up the HIL host for CI (docs/HIL.md 10, D27, D32; W3).
#
# Run after bootstrap.sh. It creates the runner user and installs the root-owned entry points:
#   user hil (system user, home /var/lib/hil), the GitHub runner and nightly run as it
#   /opt/hil/bin/run-hil        hardware half of a run; the only sudo command of user hil:
#                               'hil ALL=(root) NOPASSWD: /opt/hil/bin/run-hil' (sudoers.d/hil-run)
#   /opt/hil/bin/pre-flash.sh   Twister pre_script (R-07 OpenOCD guard), named in map.yml
#   /opt/hil/bin/nightly        nightly.sh, started by hil-nightly.timer
#   /opt/hil/hooks/job-started.sh  runner hook (ACTIONS_RUNNER_HOOK_JOB_STARTED in <runner>/.env)
#   /etc/tmpfiles.d/hil.conf    /run/hil (locks, group hil), /opt/hil/out pruned after 7 days
#   /etc/systemd/system/hil-nightly.{service,timer}
# Re-run after changing any of these files: the installed copies are what runs as root, never
# the work tree.
#
# Usage: sudo hil/host/install-runner.sh [--runner-dir DIR] [--register URL] [--nightly-repo URL]
#                                        [--enable-nightly]
#   --runner-dir DIR     the actions-runner directory (default /opt/hil/runner): the hook is
#                        added to its .env when it exists
#   --register URL       also register and start the runner there (DIR/config.sh must exist;
#                        the registration token comes from $RUNNER_TOKEN, never argv); labels
#                        hil-build,hil,bench1, service user hil
#   --nightly-repo URL   clone URL for /var/lib/hil/nightly-repo (the nightly's own clone)
#   --enable-nightly     enable hil-nightly.timer (after 'gh auth login' as hil works)
# Environment: DESTDIR (staging root for packaging and tests: files only, no users, no
# ownership, no systemctl), RUNNER_TOKEN.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin

here=$(cd "$(dirname "$0")" && pwd -P)
dest=${DESTDIR:-}
runner=/opt/hil/runner register="" nightly_repo="" enable=0
die() { echo "install-runner.sh: $*" >&2; exit 2; }
while (($#)); do
	case $1 in
	--runner-dir) runner=${2:?}; shift ;;
	--register) register=${2:?}; shift ;;
	--nightly-repo) nightly_repo=${2:?}; shift ;;
	--enable-nightly) enable=1 ;;
	-h | --help) sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
	*) die "unknown argument '$1' (see -h)" ;;
	esac
	shift
done
if [[ -z $dest ]]; then
	((EUID == 0)) || die "run as root: sudo $0"
	root=(-o root -g root)
else
	root=()
fi
system() { [[ -z $dest ]]; } # true when installing into the live system

# user hil: owns the runner, the workspace and /opt/hil/out; no login password
if system && ! getent passwd hil >/dev/null; then
	useradd --system --user-group --create-home --home-dir /var/lib/hil --shell /bin/bash hil
fi

install -d "${root[@]}" -m 0755 "$dest/opt/hil" "$dest/opt/hil/bin" "$dest/opt/hil/hooks" \
	"$dest/etc/hil" "$dest/etc/sudoers.d" "$dest/etc/tmpfiles.d" "$dest/etc/systemd/system" \
	"$dest/var/lib/hil/nightly"
install "${root[@]}" -m 0755 "$here/run-hil" "$dest/opt/hil/bin/run-hil"
install "${root[@]}" -m 0755 "$here/pre-flash.sh" "$dest/opt/hil/bin/pre-flash.sh"
install "${root[@]}" -m 0755 "$here/nightly.sh" "$dest/opt/hil/bin/nightly"
install "${root[@]}" -m 0755 "$here/job-started.sh" "$dest/opt/hil/hooks/job-started.sh"
install "${root[@]}" -m 0644 "$here/README-host.md" "$dest/opt/hil/README-host.md"
install "${root[@]}" -m 0644 "$here/hil-nightly.service" "$here/hil-nightly.timer" "$dest/etc/systemd/system/"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat >"$tmp/hil-run" <<'EOF'
# Installed by hil/host/install-runner.sh (docs/HIL.md D32). The runner user's single entry:
# the root-owned run-hil validates its arguments and the run directory. Never add
# 'ip netns exec', a shell, or any script from a work tree here.
hil ALL=(root) NOPASSWD: /opt/hil/bin/run-hil
EOF
visudo -cq -f "$tmp/hil-run"
install "${root[@]}" -m 0440 "$tmp/hil-run" "$dest/etc/sudoers.d/hil-run"

cat >"$tmp/hil.conf" <<'EOF'
# Installed by hil/host/install-runner.sh. /run/hil: bench and workspace locks (run-hil,
# ci-build.sh); /opt/hil/out: one directory per run, removed 7 days after its last change.
d /run/hil 0775 root hil -
d /opt/hil/out 0755 hil hil 7d
EOF
install "${root[@]}" -m 0644 "$tmp/hil.conf" "$dest/etc/tmpfiles.d/hil.conf"

if system; then
	systemd-tmpfiles --create /etc/tmpfiles.d/hil.conf
	chown hil:hil /var/lib/hil
	if [[ -n $nightly_repo && ! -d /var/lib/hil/nightly-repo/.git ]]; then
		runuser -u hil -- git clone --quiet "$nightly_repo" /var/lib/hil/nightly-repo
	fi
	systemctl daemon-reload
	((enable)) && systemctl enable --now hil-nightly.timer
fi

# runner hook, and optionally the registration (labels as in the workflow: hil-build, hil, bench1)
if [[ -d $dest$runner ]]; then
	env_file=$dest$runner/.env
	touch "$env_file"
	sed -i '/^ACTIONS_RUNNER_HOOK_JOB_STARTED=/d' "$env_file"
	echo "ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/hil/hooks/job-started.sh" >>"$env_file"
	system && chown hil:hil "$env_file"
	if [[ -n $register ]] && system; then
		[[ -x $runner/config.sh && -n ${RUNNER_TOKEN:-} ]] || die "--register needs $runner/config.sh and RUNNER_TOKEN"
		chown -R hil:hil "$runner"
		(cd "$runner" && runuser -u hil -- ./config.sh --unattended --replace --url "$register" \
			--token "$RUNNER_TOKEN" --name "$(hostname -s)-bench1" --labels hil-build,hil,bench1 --work _work)
		(cd "$runner" && ./svc.sh install hil && ./svc.sh start)
	fi
elif [[ -n $register ]]; then
	die "$runner does not exist: unpack the actions-runner there first (README-host.md)"
fi

echo "installed run-hil, pre-flash.sh, nightly, job-started.sh, sudoers.d/hil-run, tmpfiles and units under ${dest:-/}"
system && ! ((enable)) && echo "next: 'sudo -u hil gh auth login', then re-run with --enable-nightly"
exit 0

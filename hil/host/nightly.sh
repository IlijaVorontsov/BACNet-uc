#!/bin/bash
# nightly.sh - start a HIL run: push the HIL tip plus one run commit to hil/nightly (D27).
#
# Installed root-owned as /opt/hil/bin/nightly by install-runner.sh and run as user hil by
# hil-nightly.timer (01:17 local time). Manual HIL runs use the same script with arguments.
#   1. fetch, 'git checkout -B hil/nightly origin/<HIL branch>' in the host's own clone;
#   2. write .hil-run.env (BACNET_REF, MQTT_REF, SELECT, CYCLES, WEEKLY, CREATED), commit it and
#      'git push -f origin hil/nightly'. That push starts the workflow (build + hil jobs): push
#      runs the workflow file of the pushed ref, while schedule and workflow_dispatch need the
#      file on the default branch, which never carries it;
#   3. fallback: WAIT minutes later, if 'gh run list --branch hil/nightly' shows no run for the
#      pushed commit, it runs both halves here: hil/twister/ci-build.sh and
#      'sudo /opt/hil/bin/run-hil --local', which holds the bench lock and keeps the results
#      under /var/lib/hil/nightly/<UTC date>.
# The push token (fine-grained: contents write, actions read, this repository only) is the
# hil user's own gh login (git credential helper 'gh auth git-credential'); no job sees it.
#
# Usage: nightly [--select EXPR] [--cycles N] [--weekly | --no-weekly] [--bacnet-ref REF]
#                [--mqtt-ref REF] [--wait MIN] [--no-push] [--no-fallback]
#   --select      marker expression for --hil-select (default 'not destructive')
#   --cycles      loop count (default 20; 50 on Sundays)
#   --weekly      --timeout-multiplier 2 (default on Sundays)
#   --bacnet-ref, --mqtt-ref   firmware revisions (default: the branch tips, resolved to SHAs)
#   --wait        minutes before the fallback check (default 10)
#   --no-push     commit only and run the fallback directly (no Actions)
#   --no-fallback push only
# Configuration (/etc/hil/hil.env, via the service's EnvironmentFile or read here): HIL_REPO
# (clone, default /var/lib/hil/nightly-repo), HIL_BRANCH, BACNET_BRANCH, MQTT_BRANCH,
# HIL_OUT, HIL_BENCH_NAME.
# Exit status: 0 pushed (and a run started) or the fallback passed, 1 the fallback failed,
# 2 usage or git error.
set -euo pipefail

die() { echo "nightly: $*" >&2; exit 2; }
usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit "${1:-0}"; }
log() { echo "nightly: $*"; }

conf=${HIL_CONF:-/etc/hil/hil.env}
if [[ -r $conf ]]; then
	while IFS='=' read -r key value; do
		[[ -n ${!key:-} ]] || declare "$key=$value"
	done < <(grep -E '^(HIL_REPO|HIL_BRANCH|BACNET_BRANCH|MQTT_BRANCH|HIL_OUT|HIL_BENCH_NAME)=' "$conf")
fi
repo=${HIL_REPO:-/var/lib/hil/nightly-repo}
hil_branch=${HIL_BRANCH:-claude/hardware-in-loop-testing-x74tww}
bacnet_branch=${BACNET_BRANCH:-claude/zephyr-bacnet-stm32-162k1g}
mqtt_branch=${MQTT_BRANCH:-claude/inter-session-communication-h989ye}
out_root=${HIL_OUT:-/opt/hil/out}
machine_branch=hil/nightly

sunday=0
[[ $(date +%u) == 7 ]] && sunday=1
select="not destructive" cycles=$((sunday ? 50 : 20)) weekly=$sunday bacnet_ref="" mqtt_ref=""
wait_min=10 push=1 fallback=1
while (($#)); do
	case $1 in
	--select) select=${2?}; shift ;;
	--cycles) cycles=${2?}; shift ;;
	--weekly) weekly=1 ;;
	--no-weekly) weekly=0 ;;
	--bacnet-ref) bacnet_ref=${2?}; shift ;;
	--mqtt-ref) mqtt_ref=${2?}; shift ;;
	--wait) wait_min=${2?}; shift ;;
	--no-push) push=0 ;;
	--no-fallback) fallback=0 ;;
	-h | --help) usage 0 ;;
	*) usage 2 >&2 ;;
	esac
	shift
done
[[ $cycles =~ ^[1-9][0-9]{0,3}$ && $wait_min =~ ^[0-9]{1,3}$ ]] || die "--cycles 1..9999, --wait 0..999"
[[ $select =~ ^[A-Za-z0-9_\ ()]*$ ]] || die "--select: only marker names, 'and', 'or', 'not' and parentheses"
[[ -d $repo/.git ]] || die "$repo is not a clone (install-runner.sh --nightly-repo URL creates it)"

git=(git -C "$repo")
"${git[@]}" fetch --quiet --prune origin || die "git fetch failed in $repo"
resolve() { "${git[@]}" rev-parse --verify --quiet "$1^{commit}" || die "cannot resolve '$1'"; }
bacnet_sha=$(resolve "${bacnet_ref:-origin/$bacnet_branch}")
mqtt_sha=$(resolve "${mqtt_ref:-origin/$mqtt_branch}")
"${git[@]}" checkout --quiet --force -B "$machine_branch" "origin/$hil_branch"
"${git[@]}" clean --quiet -fdx
stamp=$(date -u +%Y%m%dT%H%M%SZ)
cat >"$repo/.hil-run.env" <<EOF
# HIL run parameters, written by /opt/hil/bin/nightly; read by the hil workflow (build job).
CREATED=$stamp
BACNET_REF=$bacnet_sha
MQTT_REF=$mqtt_sha
SELECT=$select
CYCLES=$cycles
WEEKLY=$weekly
EOF
"${git[@]}" add -f .hil-run.env
"${git[@]}" -c user.name="HIL nightly" -c user.email="hil@$(hostname -f 2>/dev/null || hostname)" \
	commit --quiet -m "HIL run $stamp: bacnet ${bacnet_sha:0:8}, mqtt ${mqtt_sha:0:8}, cycles $cycles" \
	-m "select: $select; weekly: $weekly"
run_sha=$("${git[@]}" rev-parse HEAD)
log "run commit $run_sha on $machine_branch (HIL origin/$hil_branch)"

if ((push)); then
	"${git[@]}" push --quiet --force origin "$machine_branch" || die "git push to $machine_branch failed"
	log "pushed $machine_branch; the workflow's build and hil jobs take it from here"
	((fallback)) || exit 0
	sleep $((wait_min * 60))
	if runs=$(cd "$repo" && gh run list --branch "$machine_branch" --limit 20 --json headSha --jq '.[].headSha'); then
		if grep -qx "$run_sha" <<<"$runs"; then
			log "Actions run found for $run_sha"
			exit 0
		fi
		log "no Actions run for $run_sha after $wait_min min: running it here"
	else
		log "gh run list failed (auth or network): running it here"
	fi
fi
((fallback)) || exit 0

# fallback: both halves on this host, from worktrees of the same commits
out=$out_root/nightly-$stamp
work=$(mktemp -d)
# shellcheck disable=SC2329 # invoked by the EXIT trap
cleanup() {
	for w in hil fw mq; do "${git[@]}" worktree remove --force "$work/$w" 2>/dev/null || true; done
	rm -rf "$work"
}
trap cleanup EXIT
"${git[@]}" worktree add --quiet --detach "$work/hil" "$run_sha"
"${git[@]}" worktree add --quiet --detach "$work/fw" "$bacnet_sha"
"${git[@]}" worktree add --quiet --detach "$work/mq" "$mqtt_sha"
status=0
"$work/hil/hil/twister/ci-build.sh" -o "$out" -f "$work/fw" -m "$work/mq" || status=1
if ((status == 0)); then
	args=(--local --select "$select" --cycles "$cycles")
	((weekly)) && args+=(--weekly)
	sudo -n /opt/hil/bin/run-hil "${args[@]}" "$out" || status=1
fi
log "fallback run $out finished with status $status"
exit "$status"

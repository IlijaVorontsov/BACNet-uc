#!/bin/bash
# job-started.sh - GitHub Actions runner hook of the HIL host (docs/HIL.md 10, item 5).
#
# Installed root-owned as /opt/hil/hooks/job-started.sh by install-runner.sh, which also sets
# ACTIONS_RUNNER_HOOK_JOB_STARTED to it in the runner's .env. The runner runs it as user hil
# before every job, with the job's GITHUB_WORKSPACE. It only tidies that job's own work tree:
#   - removes ws/ (the firmware checkouts of an earlier, aborted job of this repository);
#   - removes stale git lock files (*.lock under .git/) that an interrupted checkout left.
# It does no bench actions: the stimulus, hub and flashing happen inside run-hil, which holds
# the bench lock. Runner hooks have no timeout, so the work runs under 'timeout 60'. A problem
# is reported as a warning in the job log and never fails the job.
set -uo pipefail
PATH=/usr/bin:/bin

if [[ ${1:-} != --inner ]]; then
	timeout --kill-after=5 60 "$0" --inner
	rc=$?
	((rc == 124 || rc == 137)) && echo "::warning::job-started hook: timed out after 60 s"
	((rc != 0 && rc != 124 && rc != 137)) && echo "::warning::job-started hook: exit status $rc"
	exit 0
fi

work=${GITHUB_WORKSPACE:-}
# the job's own checkout: <runner>/_work/<repo>/<repo>, never anything else
if [[ -z $work || ! -d $work || $(realpath "$work") != */_work/*/* ]]; then
	echo "::warning::job-started hook: GITHUB_WORKSPACE '$work' is not a runner work tree; nothing cleaned"
	exit 0
fi
work=$(realpath "$work")
cleaned=0
if [[ -d $work/ws ]]; then
	rm -rf --one-file-system "${work:?}/ws" && cleaned=$((cleaned + 1))
fi
while IFS= read -r -d '' lockfile; do
	rm -f "$lockfile" && cleaned=$((cleaned + 1))
done < <(find "$work" -xdev -path '*/.git/*' -name '*.lock' -type f -print0 2>/dev/null)
echo "job-started hook: $work: $cleaned leftover(s) removed"

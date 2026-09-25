#!/bin/bash
# hil/tools/survey.sh - branch survey, run before every design revision (docs/HIL.md 0.1).
#
# For every branch of the remote it prints the head commit (short SHA, committer date, subject),
# the Zephyr revision pinned by the branch's west.yml and the platform_allow lists of its
# Twister files (sample.yaml, testcase.yaml). It then diffs west.yml, zephyr/module.yml and
# modules/ of the HIL branch against the BACnet tip: the HIL branch carries copies of these
# shared workspace files, and every build uses the firmware checkout's own ones (D26, C20),
# so any difference is drift and the script exits 1. The MQTT branch's drift against the
# BACnet tip is printed for information only.
#
# Usage: hil/tools/survey.sh [--fetch] [--remote R] [--hil REF] [--bacnet REF] [--mqtt REF]
#   --fetch       git fetch the remote first
#   --remote R    remote to survey (default origin)
#   --hil REF     HIL side of the drift check (default HEAD; uncommitted edits are reported)
#   --bacnet REF  BACnet tip (default R/claude/zephyr-bacnet-stm32-162k1g)
#   --mqtt REF    MQTT tip (default R/claude/inter-session-communication-h989ye)
# Run it inside the repository. Exit status: 0 no drift, 1 drift, 2 usage or git error.
set -euo pipefail

remote=origin hil=HEAD bacnet="" mqtt="" fetch=0
SHARED=(west.yml zephyr/module.yml modules)

die() { echo "survey.sh: $*" >&2; exit 2; }
while (($#)); do
	case $1 in
	--fetch) fetch=1 ;;
	--remote) remote=${2:?}; shift ;;
	--hil) hil=${2:?}; shift ;;
	--bacnet) bacnet=${2:?}; shift ;;
	--mqtt) mqtt=${2:?}; shift ;;
	-h | --help) sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
	*) die "unknown argument '$1' (see --help)" ;;
	esac
	shift
done
bacnet=${bacnet:-$remote/claude/zephyr-bacnet-stm32-162k1g}
mqtt=${mqtt:-$remote/claude/inter-session-communication-h989ye}

git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository"
((fetch)) && { git fetch --quiet --prune "$remote" || die "git fetch $remote failed"; }
for ref in "$hil" "$bacnet" "$mqtt"; do
	git rev-parse --verify --quiet "$ref^{commit}" >/dev/null || die "unknown ref '$ref'"
done

# zephyr_rev <ref>: revision of the 'zephyr' project in <ref>:west.yml ("-" without one)
zephyr_rev() {
	git show "$1:west.yml" 2>/dev/null | awk '
		/^[[:space:]]*-[[:space:]]*name:[[:space:]]*zephyr[[:space:]]*$/ { z = 1; next }
		z && /^[[:space:]]*-[[:space:]]*name:/ { z = 0 }
		z && /^[[:space:]]*revision:/ { print $2; found = 1; exit }
		END { if (!found) print "-" }'
}

# platforms <ref>: "<file>: <platform>,..." for every platform_allow list of the Twister files
platforms() {
	local f
	git ls-tree -r --name-only "$1" | { grep -E '(^|/)(sample|testcase)\.yaml$' || true; } |
		while read -r f; do
		git show "$1:$f" | awk -v file="$f" '
			function flush() { if (list != "") print "    " file ": " list; list = ""; col = -1 }
			BEGIN { col = -1 }
			col >= 0 && /^[[:space:]]*-[[:space:]]/ && match($0, /[^ ]/) > col {
				v = $0; sub(/^[[:space:]]*-[[:space:]]*/, "", v); sub(/[[:space:]]*(#.*)?$/, "", v)
				list = list (list == "" ? "" : ",") v; next }
			col >= 0 { flush() }
			/^[[:space:]]*platform_allow:/ {
				v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); sub(/[[:space:]]*(#.*)?$/, "", v)
				gsub(/[][]/, "", v); gsub(/[[:space:]]*,[[:space:]]*|[[:space:]]+/, ",", v)
				if (v != "") { list = v; flush() } else col = match($0, /[^ ]/) }
			END { flush() }'
	done
}

echo "== Branches of $remote ($(date -u +%Y-%m-%dT%H:%MZ))"
git for-each-ref --sort=-committerdate \
	--format='%(refname:short)%09%(objectname:short)%09%(committerdate:iso-strict)%09%(subject)' \
	"refs/remotes/$remote/" | while IFS=$'\t' read -r ref sha date subject; do
	[[ $ref == "$remote" || $ref == "$remote/HEAD" ]] && continue
	echo "${ref#"$remote"/} @$sha ($date) $subject"
	echo "  zephyr: $(zephyr_rev "$ref")"
	plat=$(platforms "$ref")
	if [[ -n $plat ]]; then
		printf '  platform_allow:\n%s\n' "$plat"
	else
		echo "  platform_allow: none"
	fi
done

drift=0
echo
echo "== Shared workspace files: HIL $hil vs BACnet $bacnet (${SHARED[*]})"
if git diff --quiet "$bacnet" "$hil" -- "${SHARED[@]}"; then
	echo "no drift"
else
	drift=1
	echo "DRIFT:"
	git diff --stat "$bacnet" "$hil" -- "${SHARED[@]}" | sed 's/^/  /'
	# sed reads the whole diff: 'head' would close the pipe early, and git's SIGPIPE exit (141)
	# would then end the script under pipefail before the MQTT section and the drift status
	git diff "$bacnet" "$hil" -- "${SHARED[@]}" | sed -n '1,200s/^/  | /p'
fi
if [[ $hil == HEAD ]] && ! git diff --quiet HEAD -- "${SHARED[@]}"; then
	echo "note: the work tree has uncommitted changes to these files (not part of the check)"
fi

echo
echo "== For information: MQTT $mqtt vs BACnet $bacnet"
if git diff --quiet "$bacnet" "$mqtt" -- "${SHARED[@]}"; then
	echo "no drift"
else
	git diff --stat "$bacnet" "$mqtt" -- "${SHARED[@]}" | sed 's/^/  /'
fi
exit "$drift"

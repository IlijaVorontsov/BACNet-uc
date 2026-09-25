#!/bin/bash
# hil/twister/ci-build.sh - the build half of a HIL run (docs/HIL.md 8.5, 10; D28).
#
# Snapshots the three checkouts into OUT/src, renders the Twister alt configs (gen.py), builds
# the stimulus image (hil/host/build.sh stim) and every HIL scenario with
# 'west twister --build-only', then checks each P1 DUT devicetree (hil/tools/check_catalog.py)
# and each release artifact (SEC-01).
# The hardware half, /opt/hil/bin/run-hil, runs the same scenarios with --test-only from the
# same OUT/twister/<app>.args, so -T, --alt-config-root, --tag and -O are identical by
# construction. Used by the workflow's build job and by the nightly fallback.
#
# Usage: hil/twister/ci-build.sh -o OUT -f FW -m MQ [-M MAP] [--it2] [--update] [--list]
#   -o OUT     run directory (CI: /opt/hil/out/<run id>-<attempt>); absent or empty
#   -f FW      BACnet checkout (branch claude/zephyr-bacnet-stm32-162k1g)
#   -m MQ      MQTT checkout (branch claude/inter-session-communication-h989ye)
#   -M MAP     Twister hardware map (default /etc/hil/$HIL_BENCH_NAME/map.yml)
#   --it2      also the It2 scenarios (MCUboot, variant)
#   --update   'west update --narrow' in the workspace first
#   --list     render and list the scenarios (west twister --list-tests); build nothing
# The HIL sources are the checkout this script is in.
#
# OUT layout (run-hil relies on it):
#   src/{hil,bacnet,mqtt}/  snapshots without .git, caches and the bacnet-stack build;
#                           src/<name>.rev says which commit, and whether edits were copied
#   site/ alt/ twister/     gen.py output
#   images/stim/            stimulus image (images/sizes.tsv)
#   bacnet/ mqtt/           Twister output (-O): twister.json for --test-only, build dirs
#   plain/{bacnet,mqtt}/    plain app builds (cmake only), the SEC-01 reference
#   results/build/          sizes.tsv of the scenarios, check_catalog reports, SEC-01 (sec01.xml)
#   results/<app>/          written by run-hil
#
# Workspace: WEST_WS (a west workspace whose manifest pins the same projects as FW/west.yml
# and MQ/west.yml; hil/tools/survey.sh reports drift). Builds hold /run/hil/ws.lock when that
# directory is writable, so two build jobs never update or use the workspace at once.
# Environment: /etc/hil/hil.env (or $HIL_CONF) supplies WEST_WS, HIL_BENCH_NAME, HIL_VENV and
# ZEPHYR_SDK_INSTALL_DIR unless the calling shell sets them.
# Exit status: 0 built, 1 a build or check failed, 2 usage or setup error.
set -euo pipefail

SRC_HIL=$(cd "$(dirname "$0")/../.." && pwd -P)
APPS=(bacnet mqtt)
out="" fw="" mq="" map="" it2=() update=0 list=0

die() { echo "ci-build.sh: $*" >&2; exit 2; }
usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit "${1:-0}"; }

conf=${HIL_CONF:-/etc/hil/hil.env}
if [[ -r $conf ]]; then
	while IFS='=' read -r key value; do
		[[ -n ${!key:-} ]] || export "$key=$value"
	done < <(grep -E '^(WEST_WS|HIL_BENCH_NAME|HIL_VENV|ZEPHYR_SDK_INSTALL_DIR)=' "$conf")
fi

while (($#)); do
	case $1 in
	-o) out=${2:?}; shift ;;
	-f) fw=${2:?}; shift ;;
	-m) mq=${2:?}; shift ;;
	-M) map=${2:?}; shift ;;
	--it2) it2=(--it2) ;;
	--update) update=1 ;;
	--list) list=1 ;;
	-h | --help) usage 0 ;;
	*) usage 2 >&2 ;;
	esac
	shift
done
[[ -n $out && -n $fw && -n $mq ]] || usage 2 >&2
[[ -d $fw/firmware && -d $mq/apps/mqtt_tls ]] || die "-f needs a BACnet checkout, -m an MQTT checkout"
[[ -n ${WEST_WS:-} ]] || die "WEST_WS is not set (the west workspace; see /etc/hil/hil.env)"
[[ -z ${HIL_VENV:-} ]] || PATH=$HIL_VENV/bin:$PATH
command -v west >/dev/null || die "west not found (HIL_VENV or an active Zephyr venv)"
py=$(command -v python3)
map=${map:-/etc/hil/${HIL_BENCH_NAME:-bench1}/map.yml}
if [[ ! -f $map ]]; then
	((list)) || die "hardware map $map not found (bootstrap.sh installs it; or -M)"
	map=$SRC_HIL/hil/host/map.yml.example
	echo "ci-build.sh: note: listing with $map"
fi
if [[ -e $out ]] && [[ -n $(ls -A "$out") ]]; then
	die "$out is not empty (every run gets a fresh directory: Twister would rename a reused -O)"
fi
mkdir -p "$out"
out=$(cd "$out" && pwd -P)
export ZEPHYR_BASE=$WEST_WS/zephyr

if [[ -d /run/hil && -w /run/hil ]]; then
	exec 8>/run/hil/ws.lock
	flock -w 3600 8 || die "workspace lock /run/hil/ws.lock busy for an hour"
fi
if ((update)); then
	(cd "$WEST_WS" && west update --narrow) || die "west update failed in $WEST_WS"
fi
manifest=$(cd "$WEST_WS" && west manifest --path) || die "$WEST_WS is not a west workspace"
for checkout in "$fw" "$mq"; do
	cmp -s "$checkout/west.yml" "$manifest" ||
		die "$checkout/west.yml differs from the workspace manifest $manifest (run hil/tools/survey.sh)"
done

# snapshot <dir> <name>: copy a checkout into OUT/src/<name>, so the test half (possibly on
# another runner, after later jobs re-checked-out _work) uses exactly what was built
snapshot() {
	local dst=$out/src/$2
	mkdir -p "$dst"
	tar -C "$1" --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=.mypy_cache \
		--exclude=.ruff_cache --exclude='*.egg-info' --exclude=./hil/tools/bacnet-stack \
		--exclude='./twister-out*' -cf - . | tar -C "$dst" -xf -
	{
		git -C "$1" rev-parse HEAD 2>/dev/null || echo "not a git checkout"
		if [[ -n $(git -C "$1" status --porcelain 2>/dev/null) ]]; then
			echo "dirty: uncommitted changes were copied"
		fi
	} >"$out/src/$2.rev"
}
snapshot "$SRC_HIL" hil
snapshot "$fw" bacnet
snapshot "$mq" mqtt
hil=$out/src/hil

"$py" "$hil/hil/twister/gen.py" --out "$out" --hil "$hil" --bacnet "$out/src/bacnet" \
	--mqtt "$out/src/mqtt" --hardware-map "$map" "${it2[@]}" || die "gen.py failed"

# twister <app> <args...>: west twister with the app's common arguments (OUT/twister/<app>.args)
twister() {
	local app=$1 common
	shift
	mapfile -t common <"$out/twister/$app.args"
	(cd "$WEST_WS" && west twister "${common[@]}" "$@")
}

status=0
if ((list)); then
	for app in "${APPS[@]}"; do twister "$app" --list-tests || status=1; done
	exit "$status"
fi

"$hil/hil/host/build.sh" -o "$out/images" stim || status=1
for app in "${APPS[@]}"; do
	twister "$app" --build-only --inline-logs || status=1
done

# every P1 DUT build: devicetree against the pin tables and the bench (hil/tools/check_catalog.py)
mkdir -p "$out/results/build"
bench=/etc/hil/${HIL_BENCH_NAME:-bench1}/bench.yml
[[ -r $bench ]] || bench=$hil/hil/host/bench.yml.example
check=$hil/hil/tools/check_catalog.py
while IFS= read -r -d '' pickle; do
	build=${pickle%/zephyr/edt.pickle}
	if [[ ! -f $check ]]; then
		echo "ci-build.sh: warning: $check is missing from the snapshot; catalog not checked" >&2
		break
	fi
	name=$(basename "$build") # the scenario; a sysbuild app image is <scenario>/firmware
	[[ $name == firmware ]] && name=$(basename "$(dirname "$build")").firmware
	report=$out/results/build/$name.catalog.txt
	if ! "$py" "$check" "$build" --bench "$bench" >"$report" 2>&1; then
		status=1
		sed 's/^/   | /' "$report"
	fi
done < <(find "$out/bacnet" "$out/mqtt" -path '*/zephyr/edt.pickle' ! -path '*/mcuboot/*' -print0 2>/dev/null)

# SEC-01, static: every release and variant scenario build against a plain build of the same
# app (same sources and board, no site configuration; 'west build --cmake-only' is enough)
sec01_test=$hil/hil/tests/release/test_sec01_release_guard.py
if [[ -f $sec01_test ]]; then
	sec01=()
	for app in "${APPS[@]}"; do
		mod=$out/src/$app
		app_dir=$mod/firmware
		[[ $app == mqtt ]] && app_dir=$mod/apps/mqtt_tls
		mkdir -p "$out/plain"
		(cd "$WEST_WS" && west build --cmake-only -p always -b nucleo_f767zi "$app_dir" -d "$out/plain/$app" \
			-- -DZEPHYR_EXTRA_MODULES="$mod") >"$out/plain/$app.log" 2>&1 ||
			{ status=1; echo "ci-build.sh: plain $app build failed ($out/plain/$app.log)" >&2; }
		sec01+=(--plain-build "$out/plain/$app")
		while IFS= read -r -d '' build; do sec01+=(--release-build "$build"); done < <(find "$out/$app" -type d \
			\( -name 'hil.*.release' -o -name 'hil.*.release.mtls' -o -name 'hil.*.variant' \) -print0)
	done
	(cd "$hil/hil" && PYTHONPATH=$hil/hil/src PYTHONDONTWRITEBYTECODE=1 "$py" -m pytest -q -p no:cacheprovider \
		"$sec01_test" "${sec01[@]}" --artifacts "$out/results/build/sec01" \
		--junit-xml "$out/results/build/sec01.xml") || status=1
else
	echo "ci-build.sh: warning: $sec01_test is missing from the snapshot; SEC-01 not run" >&2
fi

# status (twister.json) and size (the last linker memory table of build.log: the app image,
# also in a sysbuild build) of every scenario build
"$py" - "$out" <<'EOF' | tee "$out/results/build/sizes.tsv"
import json
import re
import sys
from pathlib import Path

out = Path(sys.argv[1])
region = re.compile(r"^\s*(FLASH|RAM|DTCM):\s+(\d+) B\s")
print("scenario\tstatus\tflash_B\tram_B\tdtcm_B\treason")
for app in ("bacnet", "mqtt"):
    report = out / app / "twister.json"
    for suite in json.loads(report.read_text()).get("testsuites", []) if report.is_file() else []:
        sizes: dict[str, str] = {}
        for log in (out / app).rglob(f"{suite['name']}/build.log"):
            for line in log.read_text(errors="replace").splitlines():
                if "Memory region" in line:
                    sizes = {}
                elif m := region.match(line):
                    sizes[m[1]] = m[2]
        cols = [suite["name"], suite.get("status", ""), *(sizes.get(r, "-") for r in ("FLASH", "RAM", "DTCM"))]
        print("\t".join([*cols, suite.get("reason") or ""]))
EOF
exit "$status"

#!/bin/bash
# hil/host/build.sh - build the rig's firmware images (docs/HIL.md 8.3) into one output directory.
#
# Usage: hil/host/build.sh [-o OUT] [-f FW] [-m MQ] [IMAGE ...]
#   -o OUT  output directory (env OUT, default ./hil-out): OUT/<image>/ is the west build dir,
#           OUT/<image>.log its log, OUT/site/ the rendered site configs, OUT/sizes.tsv the sizes
#   -f FW   BACnet checkout (env FW; branch claude/zephyr-bacnet-stm32-162k1g)
#   -m MQ   MQTT checkout (env MQ; branch claude/inter-session-communication-h989ye)
#   IMAGE   images to build (default: every image below except bac-mcuboot and mq-sil-inst)
#
#   image        tier          what
#   stim         rig           apps/hil_stimulus (nucleo_f767zi)
#   bac-rel      release       FW/firmware + site/f767-app-pool.overlay, UC_APP_POOL_SIZE=98304 (D24;
#                              only while FW's own board overlay has no DTCM pool, FW-04)
#   bac-inst     instrumented  bac-rel + -S hil -S hil-io + lib/hil
#   mq-rel       release       MQ/apps/mqtt_tls + site/mqtt-site-hil.conf
#   mq-mtls      release       mq-rel + site/mqtt-site-hil-mtls.conf
#   mq-var       variant       mq-rel + site/variant-publish120.conf (It2, MQTT-09)
#   mq-inst      instrumented  mq-rel + -S hil + lib/hil
#   mq-sil       sil           native_sim/native/64: mq-rel site + site/sil/{native-sim-tap,mqtt-sil}.conf
#   mq-sil-mtls  sil           mq-sil + site/mqtt-site-hil-mtls.conf: TLS-03's mTLS image in SIL
#   bac-sil      sil           native_sim/native/64: FW/firmware + site/sil/native-sim-tap.conf
#   bac-mcuboot  release       sysbuild with FW's overlay-mcuboot.conf + the workaround (It2, OTA-*;
#                              built only when named)
#   mq-sil-inst  instrumented  mq-sil + -S hil + lib/hil: runs lib/hil (HIL-BOOT, HIL-READY, hil
#                              shell) without hardware (built only when named)
#
# Workspace. Run inside a west workspace (the current directory or $WEST_WS) whose manifest
# pins the same projects as the checkouts: FW/west.yml and MQ/west.yml must be identical to the
# workspace manifest file, else the build stops (hil/tools/survey.sh reports the drift). Each
# firmware checkout is itself the Zephyr module "bacnet-uc" (zephyr/module.yml: dts_root,
# snippet_root, module_ext_root). build.sh passes it with -DZEPHYR_EXTRA_MODULES=<checkout>:
# zephyr_module.py keys modules by name and reads extra modules last, so the checkout replaces
# the workspace manifest repository's copy of "bacnet-uc". The build therefore uses the
# checkout's own dts/bindings (uc,io-channels), snippets and modules/ (WAMR glue), which is what
# a workspace whose manifest is the checkout gives, with no second workspace. -DDTS_ROOT=<FW>
# alone would find the binding but keep the manifest repository's modules/ glue. The HIL
# snippets then come from -DSNIPPET_ROOT=$HIL and lib/hil from a second extra module.
#
# Site configs. hil/site/*.conf name the TEST-ONLY PKI of this checkout as @HIL@/hil/pki/...;
# they are rendered with the absolute checkout path into OUT/site/ and built from there.
#
# Checks. The script fails (exit 1) when an image does not build, when a build log has a
# warning that points into HIL-owned code (apps/hil_stimulus, lib/hil, snippets, hil/), when
# the stimulus log has any warning at all, when a release or variant .config sets CONFIG_HIL*,
# when an instrumented .config lacks CONFIG_HIL=y, or when hil/tools/check_catalog.py finds a
# P1 DUT devicetree that differs from the pin tables (report in OUT/<image>.catalog.txt).
# lib/hil also compiles with -Werror. SEC-01 does the full allowlist diff.
#
# Environment: ZEPHYR_SDK_INSTALL_DIR (the SDK 1.0.1 toolchain), WEST_WS, OUT, FW, MQ,
# PYTHON (interpreter for check_catalog.py, default python3; needs PyYAML, as west does).
# bac-mcuboot signs with MCUboot's imgtool, which needs the packages of
# bootloader/mcuboot/scripts/requirements.txt (cryptography, cbor2, ...) in the west venv.
set -euo pipefail

HIL=$(cd "$(dirname "$0")/../.." && pwd -P)
OUT=${OUT:-$PWD/hil-out}
FW=${FW:-}
MQ=${MQ:-}
DEFAULT_IMAGES=(stim bac-rel bac-inst mq-rel mq-mtls mq-var mq-inst mq-sil mq-sil-mtls bac-sil)
F767=nucleo_f767zi
NSIM=native_sim/native/64
APP_POOL=(-DEXTRA_DTC_OVERLAY_FILE="$HIL/hil/site/f767-app-pool.overlay" -DCONFIG_UC_APP_POOL_SIZE=98304)

die() { echo "build.sh: $*" >&2; exit 2; }
usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit "${1:-0}"; }

while getopts "o:f:m:h" opt; do
	case $opt in
	o) OUT=$OPTARG ;;
	f) FW=$OPTARG ;;
	m) MQ=$OPTARG ;;
	h) usage 0 ;;
	*) usage 2 >&2 ;;
	esac
done
shift $((OPTIND - 1))
images=("$@")
((${#images[@]})) || images=("${DEFAULT_IMAGES[@]}")

command -v west >/dev/null || die "west not found (activate the Zephyr venv)"
[[ -n ${ZEPHYR_SDK_INSTALL_DIR:-} ]] || echo "build.sh: note: ZEPHYR_SDK_INSTALL_DIR is not set" >&2
[[ -n ${WEST_WS:-} ]] && cd "$WEST_WS"
manifest=$(west manifest --path) || die "not inside a west workspace (set WEST_WS)"
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd -P)

# checkout <var name> <dir>: an existing firmware checkout whose west.yml matches the workspace
checkout() {
	local dir=$2
	[[ -n $dir ]] || die "image needs $1 (option -${1:0:1} or env $1)"
	[[ -f $dir/west.yml && -f $dir/zephyr/module.yml ]] || die "$1=$dir is not a firmware checkout"
	cmp -s "$dir/west.yml" "$manifest" ||
		die "$1: $dir/west.yml differs from the workspace manifest $manifest (run hil/tools/survey.sh)"
	(cd "$dir" && pwd -P)
}

# Site configs with @HIL@ replaced by this checkout's absolute path.
mkdir -p "$OUT/site/sil"
for f in "$HIL"/hil/site/*.conf "$HIL"/hil/site/sil/*.conf; do
	sed "s|@HIL@|$HIL|g" "$f" >"$OUT/site/${f#"$HIL"/hil/site/}"
done
SITE=$OUT/site

# spec <image>: sets board, app, tier, west_args and cmake_args of one image
spec() {
	local mods="" conf="" extra=()
	west_args=()
	case $1 in
	stim)
		board=$F767 app=$HIL/apps/hil_stimulus tier=rig ;;
	bac-*)
		mods=$(checkout FW "$FW")
		app=$mods/firmware board=$F767 tier=release
		# FW-04: the workaround only for a checkout whose board overlay does not put the WAMR
		# pool into DTCM itself (BACnet e62a095 and later do, at 112 KiB; forcing 96 KiB there
		# would no longer build the product's release image). twister/gen.py decides the same.
		grep -qs 'uc,app-pool' "$app/boards/$F767.overlay" || extra=("${APP_POOL[@]}")
		case $1 in
		bac-rel) ;;
		bac-inst) tier=instrumented west_args=(-S hil -S hil-io) ;;
		bac-sil) board=$NSIM tier=sil extra=() conf=$SITE/sil/native-sim-tap.conf ;;
		bac-mcuboot) west_args=(--sysbuild) conf=overlay-mcuboot.conf ;;
		*) die "unknown image '$1' (see -h)" ;;
		esac ;;
	mq-*)
		mods=$(checkout MQ "$MQ")
		app=$mods/apps/mqtt_tls board=$F767 tier=release conf=$SITE/mqtt-site-hil.conf
		case $1 in
		mq-rel) ;;
		mq-mtls) conf+=";$SITE/mqtt-site-hil-mtls.conf" ;;
		mq-var) conf+=";$SITE/variant-publish120.conf" tier=variant ;;
		mq-inst) tier=instrumented west_args=(-S hil) ;;
		mq-sil) conf+=";$SITE/sil/native-sim-tap.conf;$SITE/sil/mqtt-sil.conf" board=$NSIM tier=sil ;;
		mq-sil-mtls)
			conf+=";$SITE/mqtt-site-hil-mtls.conf;$SITE/sil/native-sim-tap.conf;$SITE/sil/mqtt-sil.conf"
			board=$NSIM tier=sil ;;
		mq-sil-inst)
			conf+=";$SITE/sil/native-sim-tap.conf;$SITE/sil/mqtt-sil.conf" board=$NSIM
			tier=instrumented west_args=(-S hil) ;;
		*) die "unknown image '$1' (see -h)" ;;
		esac ;;
	*) die "unknown image '$1' (see -h)" ;;
	esac
	# instrumented images: lib/hil as a second extra module, HIL snippets from this checkout
	[[ $tier == instrumented ]] && mods+=";$HIL/lib/hil" extra+=(-DSNIPPET_ROOT="$HIL")
	cmake_args=("${extra[@]}")
	[[ -n $mods ]] && cmake_args+=(-DZEPHYR_EXTRA_MODULES="$mods")
	[[ -n $conf ]] && cmake_args+=(-DEXTRA_CONF_FILE="$conf")
	return 0
}

# sizes <app dir name> <build dir> <log>: "flash ram extra" in bytes of the application image
# (native_sim: text and data + bss of zephyr.exe; sysbuild: the table after "Performing build
# step for '<app>'", with the MCUboot image's flash in extra). ld prints a used size that is a
# whole multiple of 1 KiB, 1 MiB or 1 GiB in that unit ("96 KB"), so the unit is converted.
sizes() {
	if [[ $board == "$NSIM" ]]; then
		size -B "$2/zephyr/zephyr.exe" | awk 'NR == 2 {printf "%s %s bss=%s\n", $1, $2 + $3, $3}'
	else
		awk -v app="'$1'" '
			BEGIN { unit["B"] = 1; unit["KB"] = 1024; unit["MB"] = 1048576; unit["GB"] = 1073741824 }
			/Performing build step for / { img = index($0, app) ? "app" : "other"; next }
			/Memory region/ { cur = img == "" ? "app" : img; seen[cur] = 1
				delete t[cur ",FLASH"]; delete t[cur ",RAM"]; delete t[cur ",DTCM"] }
			$2 ~ /^[0-9]+$/ && ($3 in unit) { sub(":", "", $1); t[cur "," $1] = $2 * unit[$3] }
			END { printf "%s %s dtcm=%s%s\n", t["app,FLASH"], t["app,RAM"], t["app,DTCM"] + 0,
				("other" in seen) ? " boot_flash=" t["other,FLASH"] : "" }' "$3"
	fi
}

# hil_warnings <log> <strict>: warning lines pointing into HIL-owned code (strict: any warning)
hil_warnings() {
	if (($2)); then
		grep -E 'warning:|CMake Warning|Warning \(' "$1" || true
	else
		grep -Ei 'warning' "$1" |
			grep -F -e "$HIL/apps/" -e "$HIL/lib/" -e "$HIL/snippets/" -e "$HIL/hil/" || true
	fi
}

status=0
# sizes.tsv keeps the rows of images built by earlier runs into the same OUT
table=$OUT/sizes.tsv
[[ -f $table ]] || printf 'image\ttier\tresult\tflash_B\tram_B\textra\tbuild_dir\n' >"$table"
for image in "${images[@]}"; do
	awk -F '\t' -v img="$image" '$1 != img' "$table" >"$table.tmp" && mv "$table.tmp" "$table"
done
for image in "${images[@]}"; do
	spec "$image"
	dir=$OUT/$image log=$OUT/$image.log result=ok
	echo "== $image ($tier): west build -b $board $app ${west_args[*]} -- ${cmake_args[*]}"
	if ! west build -p always -b "$board" "$app" -d "$dir" "${west_args[@]}" -- "${cmake_args[@]}" \
		>"$log" 2>&1; then
		result=FAILED
		tail -n 25 "$log" | sed 's/^/   | /'
	fi
	strict=0
	[[ $image == stim ]] && strict=1
	warn=$(hil_warnings "$log" "$strict")
	if [[ -n $warn ]]; then
		result=${result/ok/WARNINGS}
		printf '   warning in HIL-owned code: %s\n' "${warn//$'\n'/$'\n   warning in HIL-owned code: '}"
	fi
	# the application .config (sysbuild: <dir>/<app dir name>/zephyr/.config; <dir>/zephyr/.config
	# is then sysbuild's own configuration)
	config=$dir/$(basename "$app")/zephyr/.config
	[[ -f $config ]] || config=$dir/zephyr/.config
	if [[ $result == ok ]]; then
		case $tier in
		release | variant)
			if grep -q '^CONFIG_HIL' "$config"; then
				result=CONFIG_HIL_IN_RELEASE
			fi ;;
		instrumented)
			grep -q '^CONFIG_HIL=y' "$config" || result=NO_CONFIG_HIL ;;
		esac
	fi
	# P1 DUT builds: devicetree against the pin tables and the example bench (check_catalog.py)
	if [[ $result == ok && $board == "$F767" && $tier != rig && -f ${config%/.config}/edt.pickle ]] &&
		! "${PYTHON:-python3}" "$HIL/hil/tools/check_catalog.py" "${config%/zephyr/.config}" \
			--bench "$HIL/hil/host/bench.yml.example" >"$dir.catalog.txt" 2>&1; then
		result=CATALOG
		sed 's/^/   | /' "$dir.catalog.txt"
	fi
	flash=- ram=- other=-
	[[ $result == FAILED ]] ||
		read -r flash ram other < <(sizes "$(basename "$app")" "$dir" "$log" 2>/dev/null || echo "- - -")
	[[ $result == ok ]] || status=1
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$image" "$tier" "$result" "${flash:--}" "${ram:--}" \
		"${other:--}" "$dir" >>"$table"
done

echo
awk -F '\t' '{printf "%-12s %-13s %-9s %10s %10s %-24s %s\n", $1, $2, $3, $4, $5, $6, $7}' "$table"
echo "(native_sim: flash_B = text, ram_B = data + bss of zephyr.exe)"
exit "$status"

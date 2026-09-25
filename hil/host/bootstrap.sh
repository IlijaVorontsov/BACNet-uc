#!/bin/bash
# hil/host/bootstrap.sh - set up an Ubuntu 24.04 HIL host (docs/HIL.md 8.4, 8.6, 11 W1).
#
# Idempotent; run it from a checkout of this repository, as root. Steps, in this order:
#   config     users hil (runner) and group hil-ops (operators); /etc/hil/hil.env and
#              /etc/hil/<bench>/{bench.yml,map.yml} from the examples (existing files are kept)
#   packages   apt: Zephyr build tools; tshark, dnsmasq-base, mosquitto clients, openssl,
#              sigrok-cli with the fx2lafw firmware, uhubctl, chrony, ethtool, docker.io, gh
#   sdk        Zephyr SDK 1.0.1 minimal (sha256-checked) + arm-zephyr-eabi, setup.sh -t -h -c
#   udev       60-openocd.rules from the SDK host tools, 99-hil.rules with the bench's serials
#              (/dev/hil/{dut0,stim0}, ID_MM_DEVICE_IGNORE); re-run after commissioning
#   dumpcap    capabilities for /usr/bin/dumpcap (dpkg-reconfigure wireshark-common)
#   docker     dockerd enabled, eclipse-mosquitto:2.1.2-alpine pulled (broker TLS key log)
#   bacnet     bacnet-stack tools at 54544d02, 'make bip' with BACDL=bip BBMD=full, not -j
#   venv       /opt/hil/venv (python3.12): west, hilrig with its dependencies, cbor2 and
#              jsonschema (BACnet SMP client), bacpypes3
#   workspace  /opt/hil/ws, owned by hil: a clone of this repository as the manifest repository
#              (same west.yml as the firmware branches), 'west update --narrow', the Zephyr
#              and MCUboot Python requirements, and pytest-twister-harness pip -e from
#              ZEPHYR_BASE (so every 'west twister' passes --allow-installed-plugin, D2)
#   net        hil/host/install-net-wrappers.sh: hil-net-up/down for group hil-ops. The runner
#              user hil gets no rule here: its only sudo command is run-hil (install-runner.sh)
#
# Usage: sudo hil/host/bootstrap.sh [--steps LIST] [--skip LIST] [--dut-sn SN] [--stim-sn SN]
#                                   [--rs485-sn SN] [--bench NAME] [--dry-run]
#   --steps, --skip   comma-separated step names (default: all)
#   --dut-sn, --stim-sn, --rs485-sn   ST-LINK / FTDI serials for 99-hil.rules and map.yml
#                     (default: dut.probe and stim.probe of the existing bench.yml)
#   --bench NAME      bench name (default bench1)
#   --dry-run         print the commands instead of running them
# Environment: HIL_PREFIX (/opt/hil), SDK_DIR (/opt/zephyr-sdk-1.0.1), ETC_DIR (/etc; any
# other value stages the files there and creates no users and reloads no udev rules),
# BACNET_STACK_URL (https://github.com/bacnet-stack/bacnet-stack.git). Then run
# install-runner.sh (runner user entry points, sudoers, timer).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

HIL=$(cd "$(dirname "$0")/../.." && pwd -P)
PREFIX=${HIL_PREFIX:-/opt/hil}
SDK_VERSION=1.0.1
SDK_DIR=${SDK_DIR:-/opt/zephyr-sdk-$SDK_VERSION}
ETC=${ETC_DIR:-/etc}
BACNET_SHA=54544d02e42777ec4755479267f5f624f9a10b78
BACNET_URL=${BACNET_STACK_URL:-https://github.com/bacnet-stack/bacnet-stack.git}
MOSQUITTO_IMAGE=eclipse-mosquitto:2.1.2-alpine
ALL_STEPS=(config packages sdk udev dumpcap docker bacnet venv workspace net)
BACNET_TOOLS=(bacwi bacrp bacwp bacrpm bacrfdt bacrbdt bacepics bacserv) # hilrig.bacnet, It1

steps=("${ALL_STEPS[@]}") skip=() bench=bench1 dut_sn="" stim_sn="" rs485_sn="" dry=0
die() { echo "bootstrap: $*" >&2; exit 2; }
log() { echo "== bootstrap: $*"; }
while (($#)); do
	case $1 in
	--steps) IFS=, read -ra steps <<<"${2:?}"; shift ;;
	--skip) IFS=, read -ra skip <<<"${2:?}"; shift ;;
	--dut-sn) dut_sn=${2:?}; shift ;;
	--stim-sn) stim_sn=${2:?}; shift ;;
	--rs485-sn) rs485_sn=${2:?}; shift ;;
	--bench) bench=${2:?}; shift ;;
	--dry-run) dry=1 ;;
	-h | --help) sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
	*) die "unknown argument '$1' (see -h)" ;;
	esac
	shift
done
for s in "${steps[@]}" "${skip[@]}"; do
	[[ " ${ALL_STEPS[*]} " == *" $s "* ]] || die "unknown step '$s' (${ALL_STEPS[*]})"
done
[[ $bench =~ ^[a-z0-9_-]+$ ]] || die "bad bench name '$bench'"
for sn in "$dut_sn" "$stim_sn" "$rs485_sn"; do [[ $sn =~ ^[A-Za-z0-9]*$ ]] || die "bad serial '$sn'"; done
((dry || EUID == 0)) || die "run as root: sudo $0"
BENCH_DIR=$ETC/hil/$bench

# run <cmd...>: run, or print with --dry-run
run() {
	if ((dry)); then
		printf '+'
		printf ' %q' "$@"
		printf '\n'
	else
		"$@"
	fi
}
# as_hil <cmd...>: run as the runner user when it exists (the workspace belongs to it)
as_hil() {
	if getent passwd hil >/dev/null && ((EUID == 0)); then run runuser -u hil -- "$@"; else run "$@"; fi
}
# install_new <src> <dst> [mode]: install a file unless the destination exists
install_new() {
	if [[ -e $2 ]]; then log "keeping $2"; else run install -D -m "${3:-0644}" "$1" "$2"; fi
}
# bench_serial <key>: dut.probe or stim.probe of the existing bench.yml
bench_serial() {
	[[ -r $BENCH_DIR/bench.yml ]] || return 0
	python3 - "$BENCH_DIR/bench.yml" "$1" 2>/dev/null <<'EOF' || true
import sys
import yaml
section, key = sys.argv[2].split(".")
value = (yaml.safe_load(open(sys.argv[1])) or {}).get(section, {}).get(key) or ""
print(value if str(value).isalnum() else "")
EOF
}

step_config() {
	if [[ $ETC == /etc ]]; then
		getent group hil-ops >/dev/null || run groupadd --system hil-ops
		getent passwd hil >/dev/null ||
			run useradd --system --user-group --create-home --home-dir /var/lib/hil --shell /bin/bash hil
	fi
	local tmp
	tmp=$(mktemp)
	cat >"$tmp" <<EOF
# /etc/hil/hil.env - HIL host configuration (root-owned). Read by run-hil, nightly,
# ci-build.sh and hil-nightly.service (EnvironmentFile): KEY=VALUE, no quotes, no spaces.
HIL_BENCH_NAME=$bench
HIL_OUT=$PREFIX/out
WEST_WS=$PREFIX/ws
HIL_VENV=$PREFIX/venv
ZEPHYR_SDK_INSTALL_DIR=$SDK_DIR
HIL_BACNET_BIN=$PREFIX/tools/bacnet-stack/bin
HIL_BACNET_STACK=$PREFIX/tools/bacnet-stack
HIL_NIGHTLY_DIR=/var/lib/hil/nightly
HIL_REPO=/var/lib/hil/nightly-repo
HIL_BRANCH=claude/hardware-in-loop-testing-x74tww
BACNET_BRANCH=claude/zephyr-bacnet-stm32-162k1g
MQTT_BRANCH=claude/inter-session-communication-h989ye
EOF
	install_new "$tmp" "$ETC/hil/hil.env"
	install_new "$HIL/hil/host/bench.yml.example" "$BENCH_DIR/bench.yml"
	sed -e "s|/etc/hil/bench1/|$BENCH_DIR/|" \
		-e "s|^\(  id: \).*|\1\"${dut_sn:-0671FF000000000000000000}\"|" "$HIL/hil/host/map.yml.example" >"$tmp"
	install_new "$tmp" "$BENCH_DIR/map.yml"
	rm -f "$tmp"
	[[ -n $dut_sn ]] || log "map.yml keeps the example probe id: set it (or re-run with --dut-sn) at commissioning"
}

step_packages() {
	run debconf-set-selections <<<"wireshark-common wireshark-common/install-setuid boolean true"
	run apt-get update
	run apt-get install -y --no-install-recommends \
		git cmake ninja-build gperf ccache dfu-util device-tree-compiler wget xz-utils file make gcc g++ \
		libmagic1 python3.12 python3.12-venv python3-dev python3-yaml \
		iproute2 ethtool tshark dnsmasq-base mosquitto mosquitto-clients openssl sigrok-cli \
		sigrok-firmware-fx2lafw uhubctl chrony docker.io gh sudo util-linux ca-certificates
	# the rig runs its own brokers inside netns svc; the host service is not needed
	run systemctl disable --now mosquitto.service || true
}

step_sdk() {
	if [[ -x $SDK_DIR/setup.sh && -d $SDK_DIR/gnu/arm-zephyr-eabi ]]; then
		log "SDK $SDK_DIR present"
	else
		local tmp base=https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v$SDK_VERSION
		local tarball=zephyr-sdk-${SDK_VERSION}_linux-x86_64_minimal.tar.xz
		tmp=$(mktemp -d)
		run wget -q -O "$tmp/$tarball" "$base/$tarball"
		run wget -q -O "$tmp/sha256.sum" "$base/sha256.sum"
		if ((!dry)); then
			grep " $tarball\$" "$tmp/sha256.sum" | (cd "$tmp" && sha256sum -c --quiet -) ||
				die "SDK checksum mismatch"
		fi
		run tar -xf "$tmp/$tarball" -C "$(dirname "$SDK_DIR")"
		rm -rf "$tmp"
	fi
	# -t: the toolchain, -h: host tools (OpenOCD 0.12 + Zephyr patches), -c: CMake package
	(cd "$SDK_DIR" 2>/dev/null || ((dry)) && run ./setup.sh -t arm-zephyr-eabi -h -c)
}

step_udev() {
	local rules=$SDK_DIR/hosttools/sysroots/x86_64-pokysdk-linux/usr/share/openocd/contrib/60-openocd.rules
	[[ -f $rules ]] || ((dry)) || die "$rules missing (run the sdk step: setup.sh -h)"
	run install -D -m 0644 "$rules" "$ETC/udev/rules.d/60-openocd.rules"
	dut_sn=${dut_sn:-$(bench_serial dut.probe)} stim_sn=${stim_sn:-$(bench_serial stim.probe)}
	[[ -n $dut_sn && -n $stim_sn ]] ||
		log "no ST-LINK serials yet: /dev/hil links stay inactive until 'bootstrap.sh --steps udev --dut-sn SN --stim-sn SN'"
	local tmp
	tmp=$(mktemp)
	sed -e "s/@DUT_SN@/${dut_sn:-@DUT_SN@}/" -e "s/@STIM_SN@/${stim_sn:-@STIM_SN@}/" \
		-e "s/@RS485_SN@/${rs485_sn:-@RS485_SN@}/" "$HIL/hil/host/99-hil.rules" >"$tmp"
	run install -D -m 0644 "$tmp" "$ETC/udev/rules.d/99-hil.rules"
	rm -f "$tmp"
	if [[ $ETC == /etc ]] && command -v udevadm >/dev/null; then
		run udevadm control --reload-rules
		run udevadm trigger --subsystem-match=tty --subsystem-match=usb
	fi
}

step_dumpcap() {
	run debconf-set-selections <<<"wireshark-common wireshark-common/install-setuid boolean true"
	run dpkg-reconfigure -f noninteractive wireshark-common
	((dry)) || getcap /usr/bin/dumpcap
}

step_docker() {
	run systemctl enable --now docker.service
	run docker pull "$MOSQUITTO_IMAGE"
}

step_bacnet() {
	local dir=$PREFIX/tools/bacnet-stack missing=()
	[[ -d $dir/.git ]] || run git clone --quiet "$BACNET_URL" "$dir"
	run git -C "$dir" checkout --quiet --detach "$BACNET_SHA"
	((dry)) || [[ $(git -C "$dir" rev-parse HEAD) == "$BACNET_SHA" ]] || die "$dir is not at $BACNET_SHA"
	run make -C "$dir" -s clean
	run make -C "$dir" BACDL=bip BBMD=full bip # the Makefile's own bip target; never -j
	for tool in "${BACNET_TOOLS[@]}"; do [[ -x $dir/bin/$tool ]] || missing+=("$tool"); done
	((dry || ${#missing[@]} == 0)) || die "bacnet-stack build lacks: ${missing[*]}"
	log "bacnet-stack $BACNET_SHA tools in $dir/bin"
}

step_venv() {
	local venv=$PREFIX/venv pkg
	[[ -x $venv/bin/python ]] || run python3.12 -m venv "$venv"
	run "$venv/bin/pip" install --quiet --upgrade pip
	# hilrig from a copy: a pip build writes build/ into the source tree. Runs use the run's own
	# hilrig (run-hil sets PYTHONPATH); this copy brings the dependencies and serves manual use.
	pkg=$(mktemp -d)
	cp -a "$HIL/hil/pyproject.toml" "$HIL/hil/README.md" "$HIL/hil/src" "$pkg/"
	run "$venv/bin/pip" install --quiet "west>=1.2" "${pkg}[dev]" "cbor2>=5.6" "jsonschema>=4.21" bacpypes3
	rm -rf "$pkg"
}

step_workspace() {
	local ws=$PREFIX/ws venv=$PREFIX/venv url
	if [[ ! -d $ws/.west ]]; then
		run git clone --quiet --no-hardlinks "$HIL" "$ws/BACNet-uc" # manifest 'self: path: BACNet-uc'
		if url=$(git -C "$HIL" remote get-url origin 2>/dev/null); then
			run git -C "$ws/BACNet-uc" remote set-url origin "$url"
		fi
		(cd "$ws" 2>/dev/null || ((dry)) && run "$venv/bin/west" init -l BACNet-uc)
	fi
	getent passwd hil >/dev/null && run chown -R hil:hil "$ws"
	(cd "$ws" 2>/dev/null || ((dry)) && as_hil "$venv/bin/west" update --narrow)
	run "$venv/bin/pip" install --quiet -r "$ws/zephyr/scripts/requirements.txt" \
		-r "$ws/bootloader/mcuboot/scripts/requirements.txt"
	run "$venv/bin/pip" install --quiet -e "$ws/zephyr/scripts/pylib/pytest-twister-harness"
}

step_net() {
	run env HIL_GROUP=hil-ops "$HIL/hil/host/install-net-wrappers.sh"
}

for step in "${ALL_STEPS[@]}"; do
	[[ " ${steps[*]} " == *" $step "* && " ${skip[*]} " != *" $step "* ]] || continue
	log "$step"
	"step_$step"
done
log "done; next: sudo hil/host/install-runner.sh (runner user entry points, sudoers, nightly timer)"

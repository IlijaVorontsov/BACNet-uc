#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the host validation tools for BACnet-uc WebAssembly modules:
#   <runner dir>/uc-wamr-runner        scenario runner (WAMR + host stub)
#   <runner dir>/libuc_bacnet_stub.so  "bacnet_uc" natives for iwasm --native-lib
#   <iwasm dir>/iwasm                  (--iwasm) WAMR's product-mini for Linux
# all with the WAMR feature set of the firmware (fast interpreter,
# libc-builtin, bulk memory, reference types, thread manager; no WASI,
# SIMD, JIT; software bounds checks) plus the AOT loader.
#
# usage: build.sh [--iwasm] [RUNNER_DIR]
#   RUNNER_DIR defaults to /tmp/uc-wamr-runner; iwasm is built into
#   $IWASM_DIR (default /tmp/uc-iwasm). WAMR_ROOT overrides the WAMR source
#   tree (default: <west top>/modules/lib/wamr).

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
wamr=${WAMR_ROOT:-$(cd "$here/../../../.." && pwd)/modules/lib/wamr}
runner_dir=/tmp/uc-wamr-runner
iwasm_dir=
while [ $# -gt 0 ]; do
	case "$1" in
	--iwasm)
		iwasm_dir=${IWASM_DIR:-/tmp/uc-iwasm}
		;;
	-h|--help)
		sed -n '3,15p' "$0"
		exit 0
		;;
	*)
		runner_dir=$1
		;;
	esac
	shift
done

if [ ! -f "$wamr/build-scripts/runtime_lib.cmake" ]; then
	echo "build.sh: WAMR not found at $wamr (set WAMR_ROOT)" >&2
	exit 1
fi
gen=()
if command -v ninja >/dev/null 2>&1; then
	gen=(-G Ninja)
fi

echo "== uc-wamr-runner -> $runner_dir"
cmake -S "$here" -B "$runner_dir" "${gen[@]}" -DWAMR_ROOT="$wamr" \
	-DCMAKE_BUILD_TYPE=Release >"$runner_dir.cmake.log" 2>&1 ||
	{ cat "$runner_dir.cmake.log"; exit 1; }
cmake --build "$runner_dir" -j "$(nproc 2>/dev/null || echo 2)"

if [ -n "$iwasm_dir" ]; then
	# iwasm exports its symbols so that --native-lib=libuc_bacnet_stub.so can
	# resolve the WAMR API from the executable.
	echo "== iwasm -> $iwasm_dir"
	cmake -S "$wamr/product-mini/platforms/linux" -B "$iwasm_dir" "${gen[@]}" \
		-DCMAKE_BUILD_TYPE=Release \
		-DWAMR_BUILD_TARGET=X86_64 \
		-DWAMR_BUILD_INTERP=1 -DWAMR_BUILD_FAST_INTERP=1 -DWAMR_BUILD_AOT=1 \
		-DWAMR_BUILD_JIT=0 -DWAMR_BUILD_FAST_JIT=0 \
		-DWAMR_BUILD_LIBC_BUILTIN=1 -DWAMR_BUILD_LIBC_WASI=0 \
		-DWAMR_BUILD_MULTI_MODULE=0 -DWAMR_BUILD_LIB_PTHREAD=0 \
		-DWAMR_BUILD_LIB_WASI_THREADS=0 -DWAMR_BUILD_SHARED_MEMORY=0 \
		-DWAMR_BUILD_SIMD=0 -DWAMR_BUILD_GC=0 -DWAMR_BUILD_EXCE_HANDLING=0 \
		-DWAMR_BUILD_MEMORY64=0 -DWAMR_BUILD_TAIL_CALL=0 \
		-DWAMR_BUILD_BULK_MEMORY=1 -DWAMR_BUILD_REF_TYPES=1 \
		-DWAMR_BUILD_THREAD_MGR=1 -DWAMR_DISABLE_HW_BOUND_CHECK=1 \
		-DCMAKE_C_FLAGS="-DWASM_ENABLE_HEAP_AUX_STACK_ALLOCATION=1" \
		-DCMAKE_EXE_LINKER_FLAGS="-Wl,--export-dynamic" \
		>"$iwasm_dir.cmake.log" 2>&1 || { cat "$iwasm_dir.cmake.log"; exit 1; }
	cmake --build "$iwasm_dir" --target iwasm -j "$(nproc 2>/dev/null || echo 2)"
fi

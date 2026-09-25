#!/bin/sh
# Builds thermostat.wasm with the recipe from bacnet_uc.h (needs clang and
# wasm-ld with the WebAssembly target, e.g. the Debian/Ubuntu clang and lld
# packages). The header is the vendored copy in hub/third_party.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
sdk="$here/../../../third_party"
clang --target=wasm32 -O2 -nostdlib -I"$sdk" \
	-Wl,--no-entry -Wl,--allow-undefined -Wl,--strip-debug \
	-Wl,--export=__heap_base -Wl,--export=__data_end \
	-Wl,-z,stack-size=4096 -Wl,--initial-memory=65536 \
	-o "$here/thermostat.wasm" "$here/thermostat.c"

#!/bin/sh
# Builds the demo's WebAssembly modules with the recipe from bacnet_uc.h
# (needs clang and wasm-ld with the WebAssembly target, e.g. the Debian/Ubuntu
# clang and lld packages). The header is the vendored copy in hub/third_party.
#
# thermostat.wasm: the room thermostat of the demo site.
# uc-link.wasm:    a stand-in the simulator emulates by name; see uc-link.c.
#                  Never install it on a real board.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
sdk="$here/../../../third_party"
for app in thermostat uc-link; do
	clang --target=wasm32 -O2 -nostdlib -I"$sdk" \
		-Wl,--no-entry -Wl,--allow-undefined -Wl,--strip-debug \
		-Wl,--export=__heap_base -Wl,--export=__data_end \
		-Wl,-z,stack-size=4096 -Wl,--initial-memory=65536 \
		-o "$here/$app.wasm" "$here/$app.c"
done

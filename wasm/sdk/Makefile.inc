# SPDX-License-Identifier: Apache-2.0
#
# Make fragment for BACnet-uc WebAssembly applications.
#
#   include <path to wasm/sdk>/Makefile.inc
#
#   app.wasm: main.c util.c
#   	$(UC_CC) $(UC_CCFLAGS) -o $@ $^
#
#   app.nucleo_f767zi.aot: app.wasm
#   	$(UC_AOT) --board nucleo_f767zi -o $@ $<
#
# A single-file application builds with the pattern rule below
# ("make hello.wasm" from hello.c). uc-cc passes -I, -D, -O and -W/-f
# options to clang; see "uc-cc --help".

UC_SDK := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
UC_CC ?= $(UC_SDK)/uc-cc
UC_AOT ?= $(UC_SDK)/uc-aot
UC_WASM_INFO ?= $(UC_SDK)/uc-wasm-info
UC_INCLUDE := $(UC_SDK)/include
UC_HEADERS := $(wildcard $(UC_INCLUDE)/*.h)
UC_CCFLAGS ?=

%.wasm: %.c $(UC_HEADERS)
	$(UC_CC) $(UC_CCFLAGS) -o $@ $<

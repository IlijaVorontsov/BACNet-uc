# SPDX-License-Identifier: Apache-2.0
#
# BACnet-uc WebAssembly SDK: examples, tests and validation.
#
#   make            examples -> build/<app>.wasm
#   make test       host-stub unit tests (gcc, ASan/UBSan) + SDK tooling tests
#   make aot        AOT files for the three boards -> build/aot/<board>/<app>.aot
#   make runner     WAMR runner, iwasm native lib and iwasm (under /tmp)
#   make validate   run the modules in WAMR, compare with native runs (needs runner)
#   make check      all of the above
#   make clean
#
# Variables: BUILD, PYTHON, HOST_CC, UC_CLANG (clang for uc-cc), WAMRC,
# WAMR_BUILD (runner build dir), IWASM_DIR (iwasm build dir).

WASM_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
include $(WASM_DIR)/sdk/Makefile.inc

BUILD ?= $(WASM_DIR)/build
PYTHON ?= python3
HOST_CC ?= gcc
HOST_CFLAGS ?= -std=gnu11 -O1 -g -Wall -Wextra -Wno-unused-parameter -Werror \
	-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer
WAMR_BUILD ?= /tmp/uc-wamr-runner
IWASM_DIR ?= /tmp/uc-iwasm
export IWASM_DIR

EXAMPLES := blinky thermostat alarm uc-link
BOARDS := nucleo_f767zi frdm_mcxn947/mcxn947/cpu0 native_sim/native/64

src_blinky := $(WASM_DIR)/examples/blinky/blinky.c
src_thermostat := $(WASM_DIR)/examples/thermostat/thermostat.c
src_alarm := $(WASM_DIR)/examples/alarm/alarm.c
src_uc-link := $(WASM_DIR)/examples/uc-link/uc_link.c
test_blinky := $(WASM_DIR)/examples/blinky/test_blinky.c
test_thermostat := $(WASM_DIR)/examples/thermostat/test_thermostat.c
test_alarm := $(WASM_DIR)/examples/alarm/test_alarm.c
test_uc-link := $(WASM_DIR)/examples/uc-link/test_uc_link.c
test_util := $(UC_SDK)/tests/test_util.c

STUB_DIR := $(UC_SDK)/host-stub
RUNNER_DIR := $(UC_SDK)/wamr-runner
STUB_SRC := $(STUB_DIR)/uc_stub.c $(STUB_DIR)/uc_stub_native.c
STUB_DEPS := $(STUB_SRC) $(wildcard $(STUB_DIR)/*.h) $(UC_HEADERS)
HOST_INC := -I$(UC_INCLUDE) -I$(STUB_DIR) -I$(RUNNER_DIR)
SCENARIO_SRC := $(RUNNER_DIR)/scenario.c $(RUNNER_DIR)/scenario_native.c

WASM := $(EXAMPLES:%=$(BUILD)/%.wasm)
HOST_TESTS := $(addprefix $(BUILD)/host/test_,$(EXAMPLES) util)
SCENARIOS := $(EXAMPLES:%=$(BUILD)/host/scenario_%)
RUNNER := $(WAMR_BUILD)/uc-wamr-runner
WAMRC ?= $(firstword $(wildcard /opt/wamrc/wamrc) wamrc)

.PHONY: all test host-test py-test aot runner validate check clean
.SECONDEXPANSION:

all: $(WASM)

$(BUILD)/%.wasm: $$(src_$$*) $(UC_HEADERS) $(UC_SDK)/uc-cc $(wildcard $(UC_SDK)/tools/*.py)
	@mkdir -p $(@D)
	$(UC_CC) -o $@ $(src_$*)

# --- host-stub tests --------------------------------------------------------

$(BUILD)/host/test_%: $$(src_$$*) $$(test_$$*) $(STUB_DEPS)
	@mkdir -p $(@D)
	$(HOST_CC) $(HOST_CFLAGS) $(HOST_INC) -o $@ $(src_$*) $(test_$*) $(STUB_SRC) -lm

$(BUILD)/host/scenario_%: $$(src_$$*) $(SCENARIO_SRC) $(RUNNER_DIR)/scenario.h $(STUB_DEPS)
	@mkdir -p $(@D)
	$(HOST_CC) $(HOST_CFLAGS) $(HOST_INC) -o $@ $(src_$*) $(SCENARIO_SRC) $(STUB_SRC) -lm

host-test: $(HOST_TESTS)
	@set -e; for t in $(HOST_TESTS); do echo "== $$(basename $$t)"; $$t; done

py-test:
	$(PYTHON) -m unittest discover -s $(UC_SDK)/tests -p 'test_*.py'

test: host-test py-test

# --- AOT --------------------------------------------------------------------

aot: $(WASM)
	@set -e; for b in $(BOARDS); do \
		d=$(BUILD)/aot/$$(echo $$b | tr / _); \
		for e in $(EXAMPLES); do \
			$(UC_AOT) --wamrc $(WAMRC) --board $$b -o $$d/$$e.aot $(BUILD)/$$e.wasm; \
		done; \
	done

# --- WAMR runner and validation ----------------------------------------------

$(RUNNER): $(wildcard $(RUNNER_DIR)/*.c $(RUNNER_DIR)/*.h $(RUNNER_DIR)/CMakeLists.txt) \
	   $(STUB_DEPS)
	$(RUNNER_DIR)/build.sh --iwasm $(WAMR_BUILD)

runner: $(RUNNER)

validate: $(WASM) $(SCENARIOS) $(RUNNER)
	$(PYTHON) $(RUNNER_DIR)/validate.py --build $(BUILD) --runner $(RUNNER) \
		$(if $(wildcard $(IWASM_DIR)/iwasm),--iwasm $(IWASM_DIR)/iwasm) \
		--json $(BUILD)/validate.json

check: all test aot validate

clean:
	rm -rf $(BUILD)

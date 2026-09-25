/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Command-line scenarios for the host stub, shared by the WAMR runner
 * (application = WebAssembly module) and the native scenario runner
 * (application linked in), so that both can run the same scenario and
 * their final states can be compared.
 */
#ifndef UC_SCENARIO_H
#define UC_SCENARIO_H

#include <stdbool.h>
#include <stdint.h>

#define UC_SCENARIO_MAX_STEPS 64

enum uc_step_kind {
	STEP_REMOTE,     /* remote:DEV:TYPE:INST=VALUE (with notification) */
	STEP_SILENT,     /* silent:DEV:TYPE:INST=VALUE (notification lost) */
	STEP_FAIL,       /* fail:DEV:TYPE:INST=ERR */
	STEP_LOCAL,      /* local:TYPE:INST=VALUE (input sampled) */
	STEP_WRITE,      /* write:TYPE:INST=VALUE[@PRIO] (BACnet client) */
	STEP_RELINQUISH, /* relinquish:TYPE:INST@PRIO */
	STEP_RESTART,    /* restart */
};

struct uc_step {
	uint64_t at_ms;
	enum uc_step_kind kind;
	uint32_t device;
	uint32_t type;
	uint32_t instance;
	uint32_t priority;
	double value;
};

struct uc_scenario {
	const char *module;
	uint64_t run_ms;
	uint32_t heap;
	uint32_t stack;
	uint32_t pool;
	bool dump;
	bool json;
	bool verbose;
	size_t n_steps;
	struct uc_step steps[UC_SCENARIO_MAX_STEPS];
};

/** Print the option summary. */
void uc_scenario_usage(const char *prog, bool wasm);

/** Parse argv into sc and apply the setup options (parameters, objects,
 *  remote points, IO, permissions, period) to the stub. Returns 0, or -1
 *  after printing an error. The stub must have been reset before. */
int uc_scenario_parse(int argc, char **argv, struct uc_scenario *sc);

/** Start the application and run the timeline. *start_rc receives the
 *  uc_stub_start() result (also of a restart step). Returns 0 when the
 *  application started and did not trap, 1 otherwise. */
int uc_scenario_run(const struct uc_scenario *sc, int32_t *start_rc);

#endif /* UC_SCENARIO_H */

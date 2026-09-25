/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * libuc_bacnet_stub.so - the "bacnet_uc" host functions (natives.c + host
 * stub) as an iwasm native library:
 *
 *   iwasm --native-lib=libuc_bacnet_stub.so --heap-size=8192 \
 *         --stack-size=4096 -f uc_app_init app.wasm
 *
 * iwasm calls one exported function per run; ticks and events need the
 * scenario runner (uc-wamr-runner). Parameters come from the environment:
 * UC_PARAMS="key=value;key=value". UC_STUB_VERBOSE=1 prints app log lines.
 */

#include <stdlib.h>
#include <string.h>

#include <wasm_export.h>

#include "natives.h"
#include "uc_stub.h"

int init_native_lib(void)
{
	const char *env = getenv("UC_PARAMS");
	char buf[1024];
	char *save = NULL;

	uc_stub_reset();
	if (env == NULL) {
		return 0;
	}
	strncpy(buf, env, sizeof(buf) - 1);
	buf[sizeof(buf) - 1] = '\0';
	for (char *kv = strtok_r(buf, ";", &save); kv != NULL; kv = strtok_r(NULL, ";", &save)) {
		char *eq = strchr(kv, '=');

		if (eq != NULL) {
			*eq = '\0';
			(void)uc_stub_param_set(kv, eq + 1);
		}
	}
	return 0;
}

uint32_t get_native_lib(char **p_module_name, NativeSymbol **p_native_symbols)
{
	uint32_t n;

	*p_module_name = "bacnet_uc";
	*p_native_symbols = uc_runner_natives(&n);
	return n;
}

void deinit_native_lib(void)
{
	if (getenv("UC_STUB_VERBOSE") != NULL) {
		uc_stub_dump(stderr);
	}
}

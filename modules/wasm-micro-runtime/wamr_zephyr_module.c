/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Module code that wasm_runtime_instantiate() would run (wamr_zephyr.h).
 *
 * WAMR 2.4.5 (execute_post_instantiate_functions() in
 * interpreter/wasm_runtime.c and aot/aot_runtime.c) calls these before
 * wasm_runtime_instantiate() returns, on an exec env it creates itself:
 *   - the start function (start section),
 *   - an exported "_initialize" of a WASI module (WASI is not built),
 *   - an exported "__post_instantiate" of type () -> (),
 *   - an exported "__wasm_call_ctors" of type () -> () (bulk memory).
 * The embedder can neither arm its watchdog nor set an instruction budget
 * or user data on that exec env, so such code would run unbounded (and
 * host functions would find no user data). CMakeLists.txt stops the build
 * when WAMR looks up other names.
 */

#include <stdbool.h>
#include <string.h>

#include "bh_platform.h"
#include "wasm_export.h"
#include "wasm_runtime_common.h"
#if WASM_ENABLE_INTERP != 0
#include "wasm.h"
#endif
#if WASM_ENABLE_AOT != 0
#include "aot_runtime.h"
#endif

#include "wamr_zephyr.h"

static bool module_has_start(wasm_module_t module)
{
	const WASMModuleCommon *m = (const WASMModuleCommon *)module;

#if WASM_ENABLE_INTERP != 0
	if (m->module_type == Wasm_Module_Bytecode) {
		return ((const WASMModule *)m)->start_function != (uint32)-1;
	}
#endif
#if WASM_ENABLE_AOT != 0
	if (m->module_type == Wasm_Module_AoT) {
		const AOTModule *a = (const AOTModule *)m;

		return (a->start_function != NULL) || (a->start_func_index != (uint32)-1);
	}
#endif
	/* unknown module kind: assume the worst */
	return true;
}

const char *wamr_zephyr_instantiate_code(wasm_module_t module)
{
	static const char *const names[] = {
		"_initialize",
		"__post_instantiate",
		"__wasm_call_ctors",
	};
	int32_t count;

	if (module == NULL) {
		return NULL;
	}
	if (module_has_start(module)) {
		return "start function";
	}

	count = wasm_runtime_get_export_count(module);
	for (int32_t i = 0; i < count; i++) {
		wasm_export_t exp;

		memset(&exp, 0, sizeof(exp));
		wasm_runtime_get_export_type(module, i, &exp);
		if ((exp.kind != WASM_IMPORT_EXPORT_KIND_FUNC) || (exp.name == NULL)) {
			continue;
		}
		/* any signature: WAMR calls those of type () -> () */
		for (size_t k = 0; k < sizeof(names) / sizeof(names[0]); k++) {
			if (strcmp(exp.name, names[k]) == 0) {
				return names[k];
			}
		}
	}

	return NULL;
}

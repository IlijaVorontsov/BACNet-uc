/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * uc-wamr-runner - runs a BACnet-uc WebAssembly (or x86-64 AOT) module in
 * WAMR on the host, with the "bacnet_uc" host functions served by the host
 * stub (sdk/host-stub). WAMR is configured like the firmware
 * (modules/wasm-micro-runtime): fast interpreter, libc-builtin, bulk
 * memory, reference types, thread manager, no WASI/SIMD; everything is
 * allocated from one pool as on the target.
 *
 * The load sequence follows firmware/src/apps/uc_app_mgr.c: load ->
 * every function import linked? -> instantiate(stack, heap) -> exec env ->
 * export signatures -> uc_app_api_version major 1 -> uc_app_init; then
 * ticks and events as scheduled by the stub. Host functions check guest
 * pointers like firmware/src/apps/uc_app_host_api.c (offset 0 or a range
 * outside the linear memory is UC_ERR_INVALID, not a trap).
 *
 * It reports the linear memory size (to confirm that the memory was shrunk
 * to __heap_base + app heap), the range WAMR bounds-checks, the pool block
 * that holds the linear memory, and the pool consumption of each stage.
 * Linear memories come from the pool through the firmware's allocation
 * path (zephyr_memmap.c, modules/wasm-micro-runtime/wamr_linear_memory.c).
 * Pool figures are for a 64-bit host; runtime structures are smaller on
 * the 32-bit targets.
 */

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <wasm_export.h>

#include "natives.h"
#include "scenario.h"
#include "uc_stub.h"
#include "zephyr_memmap_api.h"

#define POOL_MAX (16u * 1024u * 1024u)

enum {
	EXP_VERSION,
	EXP_INIT,
	EXP_TICK,
	EXP_COV,
	EXP_WRITE,
	EXP_DEINIT,
	EXP_COUNT
};

static const struct {
	const char *name;
	uint32_t n_params;
	uint32_t n_results;
	wasm_valkind_t params[6];
	wasm_valkind_t result;
} export_sigs[EXP_COUNT] = {
	[EXP_VERSION] = {"uc_app_api_version", 0, 1, {0}, WASM_I32},
	[EXP_INIT] = {"uc_app_init", 0, 1, {0}, WASM_I32},
	[EXP_TICK] = {"uc_app_tick", 1, 0, {WASM_I64}, 0},
	[EXP_COV] = {"uc_app_on_cov",
		     6,
		     0,
		     {WASM_I32, WASM_I32, WASM_I32, WASM_I32, WASM_I32, WASM_F64},
		     0},
	[EXP_WRITE] =
		{"uc_app_on_write", 5, 0, {WASM_I32, WASM_I32, WASM_I32, WASM_I32, WASM_F64}, 0},
	[EXP_DEINIT] = {"uc_app_deinit", 0, 0, {0}, 0},
};

static struct {
	wasm_module_inst_t inst;
	wasm_exec_env_t env;
	wasm_function_inst_t fn[EXP_COUNT];
	bool trapped;
	char exception[256];
	const char *trap_fn;
	/* for a restart: the firmware starts every run with a new instance */
	wasm_module_t module;
	uint32_t stack;
	uint32_t heap;
	bool stale;
	uint32_t instances;
} rt;

static uint8_t *pool;

/* ---------------------------------------------------------------------- */
/* Application calls                                                       */
/* ---------------------------------------------------------------------- */

static bool call(int fn, uint32_t n_results, wasm_val_t *results, uint32_t n_args, wasm_val_t *argv)
{
	const char *exc;

	if (rt.trapped) {
		return false;
	}
	if (wasm_runtime_call_wasm_a(rt.env, rt.fn[fn], n_results, results, n_args, argv)) {
		return true;
	}
	exc = wasm_runtime_get_exception(rt.inst);
	snprintf(rt.exception, sizeof(rt.exception), "%s", (exc != NULL) ? exc : "trap");
	rt.trap_fn = export_sigs[fn].name;
	rt.trapped = true;
	fprintf(stderr, "trap in %s: %s\n", rt.trap_fn, rt.exception);
	return false;
}

static wasm_val_t v_i32(uint32_t v)
{
	wasm_val_t val = {.kind = WASM_I32, .of.i32 = (int32_t)v};

	return val;
}

static wasm_val_t v_f64(double v)
{
	wasm_val_t val = {.kind = WASM_F64, .of.f64 = v};

	return val;
}

static bool instantiate(char *err, size_t err_size);

static uint32_t app_api_version(void)
{
	wasm_val_t res = {0};
	char err[128];

	if (rt.stale && !instantiate(err, sizeof(err))) {
		fprintf(stderr, "re-instantiate failed: %s\n", err);
		rt.trapped = true;
		return 0u;
	}
	return call(EXP_VERSION, 1, &res, 0, NULL) ? (uint32_t)res.of.i32 : 0u;
}

static int32_t app_init(void)
{
	wasm_val_t res = {0};

	return call(EXP_INIT, 1, &res, 0, NULL) ? res.of.i32 : -1;
}

static void app_tick(uint64_t now_ms)
{
	wasm_val_t a = {.kind = WASM_I64, .of.i64 = (int64_t)now_ms};

	(void)call(EXP_TICK, 0, NULL, 1, &a);
}

static void app_on_cov(int32_t sub_id, uint32_t device, uint32_t type, uint32_t instance,
		       uint32_t prop, double value)
{
	wasm_val_t a[6] = {v_i32((uint32_t)sub_id), v_i32(device), v_i32(type),
			   v_i32(instance),         v_i32(prop),   v_f64(value)};

	(void)call(EXP_COV, 0, NULL, 6, a);
}

static void app_on_write(uint32_t type, uint32_t instance, uint32_t prop, uint32_t priority,
			 double value)
{
	wasm_val_t a[5] = {v_i32(type), v_i32(instance), v_i32(prop), v_i32(priority),
			   v_f64(value)};

	(void)call(EXP_WRITE, 0, NULL, 5, a);
}

static void app_deinit(void)
{
	(void)call(EXP_DEINIT, 0, NULL, 0, NULL);
	rt.stale = true;
}

static bool app_failed(void)
{
	return rt.trapped;
}

static bool export_sig_ok(wasm_function_inst_t f, int i)
{
	wasm_valkind_t types[6];
	uint32_t np = wasm_func_get_param_count(f, rt.inst);
	uint32_t nr = wasm_func_get_result_count(f, rt.inst);

	if ((np != export_sigs[i].n_params) || (nr != export_sigs[i].n_results)) {
		return false;
	}
	if (np > 0u) {
		wasm_func_get_param_types(f, rt.inst, types);
		if (memcmp(types, export_sigs[i].params, np * sizeof(types[0])) != 0) {
			return false;
		}
	}
	if (nr > 0u) {
		wasm_func_get_result_types(f, rt.inst, types);
		if (types[0] != export_sigs[i].result) {
			return false;
		}
	}
	return true;
}

/* ---------------------------------------------------------------------- */
/* Main                                                                    */
/* ---------------------------------------------------------------------- */

/* (Re-)create instance and exec env and look up the exports. */
static bool instantiate(char *err, size_t err_size)
{
	if (rt.env != NULL) {
		wasm_runtime_destroy_exec_env(rt.env);
		rt.env = NULL;
	}
	if (rt.inst != NULL) {
		wasm_runtime_deinstantiate(rt.inst);
		rt.inst = NULL;
	}
	rt.stale = false;
	rt.inst = wasm_runtime_instantiate(rt.module, rt.stack, rt.heap, err, (uint32_t)err_size);
	if (rt.inst == NULL) {
		return false;
	}
	rt.env = wasm_runtime_create_exec_env(rt.inst, rt.stack);
	if (rt.env == NULL) {
		snprintf(err, err_size, "exec env: out of memory");
		return false;
	}
	rt.instances++;
	for (int i = 0; i < EXP_COUNT; i++) {
		rt.fn[i] = wasm_runtime_lookup_function(rt.inst, export_sigs[i].name);
		if ((rt.fn[i] != NULL) && !export_sig_ok(rt.fn[i], i)) {
			snprintf(err, err_size, "export %s: wrong signature", export_sigs[i].name);
			return false;
		}
	}
	if (rt.fn[EXP_VERSION] == NULL) {
		snprintf(err, err_size, "export uc_app_api_version missing");
		return false;
	}
	return true;
}

static uint32_t pool_used(void)
{
	mem_alloc_info_t mi;

	if (!wasm_runtime_get_mem_alloc_info(&mi)) {
		return 0;
	}
	return mi.total_size - mi.total_free_size;
}

static uint8_t *read_file(const char *path, uint32_t *size)
{
	FILE *f = fopen(path, "rb");
	uint8_t *buf = NULL;
	long n;

	if (f == NULL) {
		return NULL;
	}
	if ((fseek(f, 0, SEEK_END) == 0) && ((n = ftell(f)) > 0) && (fseek(f, 0, SEEK_SET) == 0)) {
		/* from the pool, like the firmware's module image */
		buf = wasm_runtime_malloc((uint32_t)n);
		if ((buf != NULL) && (fread(buf, 1, (size_t)n, f) != (size_t)n)) {
			wasm_runtime_free(buf);
			buf = NULL;
		}
		*size = (uint32_t)n;
	}
	fclose(f);
	return buf;
}

static int32_t global_i32(const char *name)
{
	wasm_global_inst_t g;

	if (!wasm_runtime_get_export_global_inst(rt.inst, name, &g) || (g.kind != WASM_I32)) {
		return -1;
	}
	return *(int32_t *)g.global_data;
}

int main(int argc, char **argv)
{
	static const struct uc_stub_app app = {
		.api_version = app_api_version,
		.init = app_init,
		.tick = app_tick,
		.on_cov = app_on_cov,
		.on_write = app_on_write,
		.deinit = app_deinit,
		.failed = app_failed,
	};
	struct uc_scenario sc;
	RuntimeInitArgs init;
	char err[256];
	wasm_module_t module = NULL;
	wasm_memory_inst_t mem;
	uint8_t *image;
	uint32_t image_size = 0;
	uint32_t used_init, used_image, used_load, used_inst, used_env;
	uint64_t linear = 0;
	uint64_t block = 0;
	uint64_t app_start = 0, app_end = 0;
	uint32_t pages = 0, page_bytes = 0;
	int32_t heap_base_after;
	int32_t start_rc = 0;
	const char *format;
	NativeSymbol *natives;
	uint32_t n_natives;
	int result;

	uc_stub_set_app(&app);
	uc_stub_reset();
	if ((uc_scenario_parse(argc, argv, &sc) < 0) || (sc.module == NULL)) {
		uc_scenario_usage(argv[0], true);
		return 2;
	}
	if ((sc.pool < 16384u) || (sc.pool > POOL_MAX)) {
		fprintf(stderr, "--pool must be 16384..%u\n", POOL_MAX);
		return 2;
	}
	/* an endless loop in a module ends the run instead of hanging make */
	alarm(120);

	pool = calloc(1, sc.pool);
	memset(&init, 0, sizeof(init));
	init.mem_alloc_type = Alloc_With_Pool;
	init.mem_alloc_option.pool.heap_buf = pool;
	init.mem_alloc_option.pool.heap_size = sc.pool;
	if ((pool == NULL) || !wasm_runtime_full_init(&init)) {
		fprintf(stderr, "WAMR init failed\n");
		return 2;
	}
	wasm_runtime_set_log_level(WASM_LOG_LEVEL_WARNING);
	natives = uc_runner_natives(&n_natives);
	if (!wasm_runtime_register_natives("bacnet_uc", natives, n_natives)) {
		fprintf(stderr, "registering natives failed\n");
		return 2;
	}
	used_init = pool_used();

	image = read_file(sc.module, &image_size);
	if (image == NULL) {
		fprintf(stderr, "%s: cannot read (or pool too small)\n", sc.module);
		return 2;
	}
	used_image = pool_used();
	format = (memcmp(image, "\0aot", 4) == 0) ? "aot" : "wasm";

	err[0] = '\0';
	module = wasm_runtime_load(image, image_size, err, sizeof(err));
	if (module == NULL) {
		fprintf(stderr, "%s: load failed: %s\n", sc.module, err);
		return 1;
	}
	used_load = pool_used();
	for (int32_t i = 0; i < wasm_runtime_get_import_count(module); i++) {
		wasm_import_t imp;

		memset(&imp, 0, sizeof(imp));
		wasm_runtime_get_import_type(module, i, &imp);
		if ((imp.kind == WASM_IMPORT_EXPORT_KIND_FUNC) && !imp.linked) {
			fprintf(stderr, "%s: unresolved import %s.%s\n", sc.module, imp.module_name,
				imp.name);
			return 1;
		}
	}

	rt.module = module;
	rt.stack = sc.stack;
	rt.heap = sc.heap;
	rt.inst = wasm_runtime_instantiate(module, sc.stack, sc.heap, err, sizeof(err));
	if (rt.inst == NULL) {
		fprintf(stderr, "%s: instantiate failed: %s\n", sc.module, err);
		return 1;
	}
	used_inst = pool_used();
	block = uc_runner_linear_block_bytes();
	rt.env = wasm_runtime_create_exec_env(rt.inst, sc.stack);
	if (rt.env == NULL) {
		fprintf(stderr, "%s: exec env: out of memory\n", sc.module);
		return 1;
	}
	used_env = pool_used();
	rt.instances = 1;

	mem = wasm_runtime_get_memory(rt.inst, 0);
	if (mem != NULL) {
		pages = (uint32_t)wasm_memory_get_cur_page_count(mem);
		page_bytes = (uint32_t)wasm_memory_get_bytes_per_page(mem);
		linear = (uint64_t)pages * page_bytes;
	}
	heap_base_after = global_i32("__heap_base");
	/* the range WAMR accepts for accesses (memory_data_size) */
	(void)wasm_runtime_get_app_addr_range(rt.inst, 0, &app_start, &app_end);

	for (int i = 0; i < EXP_COUNT; i++) {
		rt.fn[i] = wasm_runtime_lookup_function(rt.inst, export_sigs[i].name);
		if ((rt.fn[i] != NULL) && !export_sig_ok(rt.fn[i], i)) {
			fprintf(stderr, "%s: export %s: wrong signature\n", sc.module,
				export_sigs[i].name);
			return 1;
		}
	}
	if (rt.fn[EXP_VERSION] == NULL) {
		fprintf(stderr, "%s: export uc_app_api_version missing\n", sc.module);
		return 1;
	}
	{
		/* NULL members are "not exported" for the stub */
		static struct uc_stub_app bound;

		bound = app;
		bound.init = (rt.fn[EXP_INIT] != NULL) ? app.init : NULL;
		bound.tick = (rt.fn[EXP_TICK] != NULL) ? app.tick : NULL;
		bound.on_cov = (rt.fn[EXP_COV] != NULL) ? app.on_cov : NULL;
		bound.on_write = (rt.fn[EXP_WRITE] != NULL) ? app.on_write : NULL;
		bound.deinit = (rt.fn[EXP_DEINIT] != NULL) ? app.deinit : NULL;
		uc_stub_set_app(&bound);
	}

	result = uc_scenario_run(&sc, &start_rc);

	if (sc.dump) {
		printf("start %d\n", (int)start_rc);
		uc_stub_dump(stdout);
		uc_stub_stop();
		printf("--- after stop\n");
		uc_stub_dump(stdout);
	} else {
		uc_stub_stop();
	}
	if (rt.trapped) {
		result = 1;
	}

	if (sc.json) {
		printf("{\"module\": \"%s\", \"format\": \"%s\", \"file_bytes\": %" PRIu32
		       ", \"stack\": %" PRIu32 ", \"heap\": %" PRIu32 ", \"pages\": %" PRIu32
		       ", \"bytes_per_page\": %" PRIu32 ", \"linear_memory_bytes\": %" PRIu64
		       ", \"bounds_bytes\": %" PRIu64 ", \"linear_block_bytes\": %" PRIu64
		       ", \"heap_base_after\": %" PRId32
		       ", \"pool_bytes\": %" PRIu32 ", \"pool_runtime\": %" PRIu32
		       ", \"pool_image\": %" PRIu32 ", \"pool_module\": %" PRIu32
		       ", \"pool_instance\": %" PRIu32 ", \"pool_exec_env\": %" PRIu32
		       ", \"start_rc\": %d, \"trapped\": %s"
		       ", \"trap\": \"%s\", \"ticks\": %" PRIu32 ", \"events\": %" PRIu32
		       ", \"errors\": %" PRIu32 ", \"log_lines\": %zu, \"result\": %d}\n",
		       sc.module, format, image_size, sc.stack, sc.heap, pages, page_bytes, linear,
		       app_end, block, heap_base_after, sc.pool, used_init, used_image - used_init,
		       used_load - used_image, used_inst - used_load, used_env - used_inst,
		       (int)start_rc, rt.trapped ? "true" : "false", rt.trapped ? rt.exception : "",
		       uc_stub_ticks(), uc_stub_events(), uc_stub_errors(), uc_stub_log_count(),
		       result);
	} else if (!sc.dump) {
		printf("%s (%s, %" PRIu32 " B): linear memory %" PRIu64 " B (%" PRIu32 " x %" PRIu32
		       " B, heap %" PRIu32 " B, bounds %" PRIu64 " B, block %" PRIu64
		       " B), pool: runtime %" PRIu32
		       ", image %" PRIu32 ", module %" PRIu32 ", instance %" PRIu32
		       ", exec env %" PRIu32 " B; start %d, %" PRIu32 " ticks, %" PRIu32
		       " events, %" PRIu32 " errors%s%s\n",
		       sc.module, format, image_size, linear, pages, page_bytes, sc.heap, app_end,
		       block, used_init, used_image - used_init, used_load - used_image,
		       used_inst - used_load, used_env - used_inst, (int)start_rc, uc_stub_ticks(),
		       uc_stub_events(), uc_stub_errors(), rt.trapped ? ", TRAP: " : "",
		       rt.trapped ? rt.exception : "");
	}

	if (rt.env != NULL) {
		wasm_runtime_destroy_exec_env(rt.env);
	}
	if (rt.inst != NULL) {
		wasm_runtime_deinstantiate(rt.inst);
	}
	wasm_runtime_unload(module);
	wasm_runtime_free(image);
	wasm_runtime_destroy();
	free(pool);
	return result;
}

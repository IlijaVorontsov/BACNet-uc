/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * "bacnet_uc" host functions for WAMR on the host, served by the host
 * stub (sdk/host-stub). Guest pointers are checked like
 * firmware/src/apps/uc_app_host_api.c does it: offset 0 or a range that
 * leaves the linear memory is passed to the stub as NULL, which answers
 * UC_ERR_INVALID (no trap). Doubles are copied with memcpy (the guest may
 * pass unaligned addresses). The signature strings are the firmware's.
 */

#include <string.h>

#include <wasm_export.h>

#include "natives.h"
#include "uc_stub.h"

/* Native address of [off, off + len) in the linear memory, NULL when the
 * range is empty, starts at 0 or leaves the memory. */
static void *app_mem(wasm_exec_env_t env, uint32_t off, uint32_t len)
{
	wasm_module_inst_t inst = wasm_runtime_get_module_inst(env);
	uint64_t start;
	uint64_t end;

	if ((off == 0u) || (len == 0u)) {
		return NULL;
	}
	if (!wasm_runtime_get_app_addr_range(inst, off, &start, &end)) {
		return NULL;
	}
	if ((uint64_t)off + len > end) {
		return NULL;
	}
	return wasm_runtime_addr_app_to_native(inst, off);
}

static void *app_opt(wasm_exec_env_t env, uint32_t off, uint32_t len)
{
	return (len > 0u) ? app_mem(env, off, len) : NULL;
}

static int32_t put_double(void *dst, double v, int32_t rc)
{
	if ((rc == UC_OK) && (dst != NULL)) {
		memcpy(dst, &v, sizeof(v));
	}
	return rc;
}

static void h_log(wasm_exec_env_t env, int32_t level, uint32_t msg, uint32_t len)
{
	uc_log(level, app_mem(env, msg, len), len);
}

static uint64_t h_uptime_ms(wasm_exec_env_t env)
{
	(void)env;
	return uc_uptime_ms();
}

static int32_t h_set_tick_period(wasm_exec_env_t env, uint32_t period_ms)
{
	(void)env;
	return uc_set_tick_period(period_ms);
}

static int32_t h_param_get(wasm_exec_env_t env, uint32_t key, uint32_t key_len, uint32_t buf,
			   uint32_t buf_len)
{
	return uc_param_get(app_mem(env, key, key_len), key_len, app_opt(env, buf, buf_len),
			    buf_len);
}

static int32_t h_param_get_number(wasm_exec_env_t env, uint32_t key, uint32_t key_len, uint32_t out)
{
	void *dst = app_mem(env, out, sizeof(double));
	double v = 0.0;

	return put_double(dst, v,
			  uc_param_get_number(app_mem(env, key, key_len), key_len,
					      (dst != NULL) ? &v : NULL));
}

static int32_t h_obj_create(wasm_exec_env_t env, uint32_t type, uint32_t instance, uint32_t name,
			    uint32_t name_len)
{
	return uc_obj_create(type, instance, app_mem(env, name, name_len), name_len);
}

static int32_t h_obj_delete(wasm_exec_env_t env, uint32_t type, uint32_t instance)
{
	(void)env;
	return uc_obj_delete(type, instance);
}

static int32_t h_prop_read(wasm_exec_env_t env, uint32_t type, uint32_t instance, uint32_t prop,
			   int32_t index, uint32_t out)
{
	void *dst = app_mem(env, out, sizeof(double));
	double v = 0.0;

	return put_double(dst, v,
			  uc_prop_read(type, instance, prop, index, (dst != NULL) ? &v : NULL));
}

static int32_t h_prop_write(wasm_exec_env_t env, uint32_t type, uint32_t instance, uint32_t prop,
			    int32_t index, double value, uint32_t priority)
{
	(void)env;
	return uc_prop_write(type, instance, prop, index, value, priority);
}

static int32_t h_prop_write_null(wasm_exec_env_t env, uint32_t type, uint32_t instance,
				 uint32_t prop, uint32_t priority)
{
	(void)env;
	return uc_prop_write_null(type, instance, prop, priority);
}

static int32_t h_prop_write_string(wasm_exec_env_t env, uint32_t type, uint32_t instance,
				   uint32_t prop, uint32_t str, uint32_t len)
{
	return uc_prop_write_string(type, instance, prop, app_opt(env, str, len), len);
}

static int32_t h_remote_read(wasm_exec_env_t env, uint32_t device, uint32_t type, uint32_t instance,
			     uint32_t prop, int32_t index, uint32_t out, uint32_t timeout_ms)
{
	void *dst = app_mem(env, out, sizeof(double));
	double v = 0.0;

	return put_double(dst, v,
			  uc_remote_read(device, type, instance, prop, index,
					 (dst != NULL) ? &v : NULL, timeout_ms));
}

static int32_t h_remote_write(wasm_exec_env_t env, uint32_t device, uint32_t type,
			      uint32_t instance, uint32_t prop, int32_t index, double value,
			      uint32_t priority, uint32_t timeout_ms)
{
	(void)env;
	return uc_remote_write(device, type, instance, prop, index, value, priority, timeout_ms);
}

static int32_t h_remote_write_null(wasm_exec_env_t env, uint32_t device, uint32_t type,
				   uint32_t instance, uint32_t prop, uint32_t priority,
				   uint32_t timeout_ms)
{
	(void)env;
	return uc_remote_write_null(device, type, instance, prop, priority, timeout_ms);
}

static int32_t h_cov_subscribe(wasm_exec_env_t env, uint32_t device, uint32_t type,
			       uint32_t instance, uint32_t lifetime_s)
{
	(void)env;
	return uc_cov_subscribe(device, type, instance, lifetime_s);
}

static int32_t h_cov_unsubscribe(wasm_exec_env_t env, int32_t sub_id)
{
	(void)env;
	return uc_cov_unsubscribe(sub_id);
}

static int32_t h_io_find(wasm_exec_env_t env, uint32_t name, uint32_t len)
{
	return uc_io_find(app_mem(env, name, len), len);
}

static int32_t h_io_read(wasm_exec_env_t env, int32_t channel, uint32_t out)
{
	void *dst = app_mem(env, out, sizeof(double));
	double v = 0.0;

	return put_double(dst, v, uc_io_read(channel, (dst != NULL) ? &v : NULL));
}

static int32_t h_io_write(wasm_exec_env_t env, int32_t channel, double value)
{
	(void)env;
	return uc_io_write(channel, value);
}

static int32_t h_kv_get(wasm_exec_env_t env, uint32_t key, uint32_t key_len, uint32_t buf,
			uint32_t buf_len)
{
	return uc_kv_get(app_mem(env, key, key_len), key_len, app_opt(env, buf, buf_len), buf_len);
}

static int32_t h_kv_set(wasm_exec_env_t env, uint32_t key, uint32_t key_len, uint32_t val,
			uint32_t val_len)
{
	return uc_kv_set(app_mem(env, key, key_len), key_len, app_opt(env, val, val_len), val_len);
}

/* Same signature strings as the firmware (uc_app_host_api.c). WAMR sorts
 * the table in place, so it is not const. */
static NativeSymbol natives[] = {
	{"uc_log", (void *)h_log, "(iii)", NULL},
	{"uc_uptime_ms", (void *)h_uptime_ms, "()I", NULL},
	{"uc_set_tick_period", (void *)h_set_tick_period, "(i)i", NULL},
	{"uc_param_get", (void *)h_param_get, "(iiii)i", NULL},
	{"uc_param_get_number", (void *)h_param_get_number, "(iii)i", NULL},
	{"uc_obj_create", (void *)h_obj_create, "(iiii)i", NULL},
	{"uc_obj_delete", (void *)h_obj_delete, "(ii)i", NULL},
	{"uc_prop_read", (void *)h_prop_read, "(iiiii)i", NULL},
	{"uc_prop_write", (void *)h_prop_write, "(iiiiFi)i", NULL},
	{"uc_prop_write_null", (void *)h_prop_write_null, "(iiii)i", NULL},
	{"uc_prop_write_string", (void *)h_prop_write_string, "(iiiii)i", NULL},
	{"uc_remote_read", (void *)h_remote_read, "(iiiiiii)i", NULL},
	{"uc_remote_write", (void *)h_remote_write, "(iiiiiFii)i", NULL},
	{"uc_remote_write_null", (void *)h_remote_write_null, "(iiiiii)i", NULL},
	{"uc_cov_subscribe", (void *)h_cov_subscribe, "(iiii)i", NULL},
	{"uc_cov_unsubscribe", (void *)h_cov_unsubscribe, "(i)i", NULL},
	{"uc_io_find", (void *)h_io_find, "(ii)i", NULL},
	{"uc_io_read", (void *)h_io_read, "(ii)i", NULL},
	{"uc_io_write", (void *)h_io_write, "(iF)i", NULL},
	{"uc_kv_get", (void *)h_kv_get, "(iiii)i", NULL},
	{"uc_kv_set", (void *)h_kv_set, "(iiii)i", NULL},
};

NativeSymbol *uc_runner_natives(uint32_t *count)
{
	*count = sizeof(natives) / sizeof(natives[0]);
	return natives;
}

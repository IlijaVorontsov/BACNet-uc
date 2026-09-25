/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host functions of import module "bacnet_uc" (wasm/sdk/include/bacnet_uc.h).
 *
 * Every function runs in the calling application's thread; the slot comes
 * from the exec env's user data. Permissions are those of the app's
 * apps.json entry. Errors are returned as UC_ERR_* (uc_err_to_api()) and
 * counted in the app status ("errors"), except UC_ERR_NOT_FOUND of the
 * lookups uc_param_get, uc_param_get_number and uc_kv_get: a missing key
 * is how an app learns to use its default.
 *
 * Pointers
 *   Pointer arguments are declared as plain i32 ("i") in the WAMR
 *   signatures and checked here against the instance's linear memory
 *   (wasm_runtime_get_app_addr_range(), which does not raise a WASM
 *   exception), so that a bad pointer or length returns UC_ERR_INVALID as
 *   bacnet_uc.h specifies instead of trapping. The whole range of every
 *   buffer (and all 8 bytes of a double *) is checked; offset 0 (NULL) is
 *   rejected. Linear memory is accessed with memcpy only (unaligned
 *   guest pointers on Cortex-M).
 *
 * Blocking
 *   Remote BACnet requests and kv file access pause the app's watchdog
 *   while they block. Local object access goes through the BACnet executor
 *   and is short.
 */

#include <errno.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/fs/fs.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include <wasm_export.h>

#include "bacnet/bacdef.h"
#include "bacnet/bacapp.h"
#include "bacnet/bacenum.h"
#include "bacnet/bacstr.h"

#include "uc/uc_apps.h"
#include "uc/uc_bacnet.h"
#include "uc/uc_common.h"
#include "uc/uc_config.h"
#include "uc/uc_io.h"
#include "uc/uc_storage.h"
#include "uc_app_internal.h"

/* Guest ABI (UC_ERR_*, constants). Its guest-side io prototypes collide
 * with uc_io.h, so they are renamed while it is included. */
#define uc_io_find  uc_guest_io_find
#define uc_io_read  uc_guest_io_read
#define uc_io_write uc_guest_io_write
#include "../../../wasm/sdk/include/bacnet_uc.h"
#undef uc_io_find
#undef uc_io_read
#undef uc_io_write

/* Log source of the applications' own log lines ("<app>: <message>"). */
LOG_MODULE_REGISTER(uc_app, CONFIG_UC_LOG_LEVEL);

BUILD_ASSERT(UC_DEVICE_LOCAL == UC_BN_DEVICE_LOCAL, "local device marker mismatch");
BUILD_ASSERT((UC_LOG_ERR == LOG_LEVEL_ERR) && (UC_LOG_WRN == LOG_LEVEL_WRN) &&
		     (UC_LOG_INF == LOG_LEVEL_INF) && (UC_LOG_DBG == LOG_LEVEL_DBG),
	     "log levels mismatch");

/* Longest log line taken from an app (longer messages are truncated). */
#define APP_LOG_LINE_MAX  120
/* Log lines per app and window; the excess is dropped and counted. */
#define APP_LOG_BURST     20
#define APP_LOG_WINDOW_MS 1000

/* Key lengths without NUL (bacnet_uc.h, apps.schema.json). */
#define APP_PARAM_KEY_MAX (sizeof(((struct uc_app_param *)0)->key) - 1)
#define APP_KV_KEY_MAX    31

/* Suffix of the temporary file of a kv write. '~' is not a key character,
 * so the temporary file never collides with another key. */
#define APP_KV_TMP_SUFFIX "~"
/* "/lfs/data/<app>/<key>~" */
#define APP_KV_PATH_MAX (sizeof(UC_DIR_DATA) + UC_APP_NAME_MAX + APP_KV_KEY_MAX + 8)
/* Upper bound of entries uc_app_kv_remove_all() deletes. */
#define APP_KV_REMOVE_MAX 256

/* ---------------------------------------------------------------------- */
/* Helpers                                                                 */
/* ---------------------------------------------------------------------- */

static inline struct uc_app_slot *slot_of(wasm_exec_env_t env)
{
	return wasm_runtime_get_user_data(env);
}

/* Count an error of the running instance and pass the code through. */
static int32_t ret_api(struct uc_app_slot *s, int32_t rc)
{
	if (rc < 0) {
		atomic_inc(&s->errors);
	}
	return rc;
}

static int32_t ret_errno(struct uc_app_slot *s, int err)
{
	return ret_api(s, (int32_t)uc_err_to_api(err));
}

/* Result of a lookup (uc_param_get*, uc_kv_get): a missing key is an
 * answer the app expects (defaults), not a failure, and is not counted. */
static int32_t ret_lookup(struct uc_app_slot *s, int32_t rc)
{
	return (rc == UC_ERR_NOT_FOUND) ? rc : ret_api(s, rc);
}

static bool has_perm(const struct uc_app_slot *s, uint32_t perm)
{
	return (s->cfg.perms & perm) == perm;
}

/* Native address of [off, off + len) in the app's linear memory, NULL when
 * the range is empty, starts at 0 or leaves the memory. */
static void *app_mem(wasm_exec_env_t env, uint32_t off, uint32_t len)
{
	wasm_module_inst_t inst = wasm_runtime_get_module_inst(env);
	uint64_t start;
	uint64_t end;

	if ((off == 0U) || (len == 0U)) {
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

/* Copy a (pointer, length) string argument into a NUL-terminated buffer.
 * -EINVAL for an empty, too long or unreadable string or embedded NULs. */
static int app_str(wasm_exec_env_t env, uint32_t off, uint32_t len, char *buf, size_t size)
{
	const char *src;

	if ((len == 0U) || (len >= size)) {
		return -EINVAL;
	}
	src = app_mem(env, off, len);
	if (src == NULL) {
		return -EINVAL;
	}
	memcpy(buf, src, len);
	buf[len] = '\0';

	return (strlen(buf) == len) ? 0 : -EINVAL;
}

/* Output double: returns the native address after checking all 8 bytes. */
static void *app_out_double(wasm_exec_env_t env, uint32_t off)
{
	return app_mem(env, off, sizeof(double));
}

static void put_double(void *dst, double v)
{
	memcpy(dst, &v, sizeof(v));
}

/* This device: UC_DEVICE_LOCAL or the local device instance. */
static bool device_is_local(uint32_t device)
{
	struct uc_bn_status st;

	if (device == UC_DEVICE_LOCAL) {
		return true;
	}
	uc_bn_status_get(&st);

	return device == st.device_instance;
}

static bool object_id_ok(uint32_t type, uint32_t instance)
{
	return (type < MAX_BACNET_OBJECT_TYPE) && (instance <= BACNET_MAX_INSTANCE);
}

/* ---------------------------------------------------------------------- */
/* Logging, time, scheduling                                               */
/* ---------------------------------------------------------------------- */

static void h_log(wasm_exec_env_t env, int32_t level, uint32_t msg, uint32_t len)
{
	struct uc_app_slot *s = slot_of(env);
	char line[APP_LOG_LINE_MAX + 1];
	const char *src;
	int64_t now = k_uptime_get();
	uint32_t n;

	if (len == 0U) {
		return;
	}
	src = app_mem(env, msg, len);
	if (src == NULL) {
		(void)ret_api(s, UC_ERR_INVALID);
		return;
	}

	if ((now - s->log_window_ms) >= APP_LOG_WINDOW_MS) {
		if (s->log_dropped > 0U) {
			LOG_WRN("%s: %u log lines dropped", s->cfg.name, s->log_dropped);
		}
		s->log_window_ms = now;
		s->log_count = 0;
		s->log_dropped = 0;
	}
	if (s->log_count >= APP_LOG_BURST) {
		s->log_dropped++;
		return;
	}
	s->log_count++;

	n = MIN(len, (uint32_t)APP_LOG_LINE_MAX);
	memcpy(line, src, n);
	while ((n > 0U) && ((line[n - 1] == '\n') || (line[n - 1] == '\r'))) {
		n--;
	}
	line[n] = '\0';
	for (uint32_t i = 0; i < n; i++) {
		unsigned char c = (unsigned char)line[i];

		if ((c < 0x20U) || (c == 0x7FU)) {
			line[i] = ' ';
		}
	}

	/* deferred logging copies the strings into the log message */
	if (level <= UC_LOG_ERR) {
		LOG_ERR("%s: %s", s->cfg.name, line);
	} else if (level == UC_LOG_WRN) {
		LOG_WRN("%s: %s", s->cfg.name, line);
	} else if (level == UC_LOG_INF) {
		LOG_INF("%s: %s", s->cfg.name, line);
	} else {
		LOG_DBG("%s: %s", s->cfg.name, line);
	}
}

static uint64_t h_uptime_ms(wasm_exec_env_t env)
{
	ARG_UNUSED(env);

	return (uint64_t)k_uptime_get();
}

static int32_t h_set_tick_period(wasm_exec_env_t env, uint32_t period_ms)
{
	struct uc_app_slot *s = slot_of(env);

	if ((period_ms != 0U) &&
	    ((period_ms < UC_APP_PERIOD_MIN_MS) || (period_ms > UC_APP_PERIOD_MAX_MS))) {
		return ret_api(s, UC_ERR_INVALID);
	}
	s->period_ms = period_ms;
	s->period_changed = true;

	return UC_OK;
}

/* ---------------------------------------------------------------------- */
/* Parameters                                                              */
/* ---------------------------------------------------------------------- */

static const char *param_lookup(wasm_exec_env_t env, uint32_t key_off, uint32_t key_len,
				int32_t *err)
{
	struct uc_app_slot *s = slot_of(env);
	char key[APP_PARAM_KEY_MAX + 1];
	const char *value;

	if ((app_str(env, key_off, key_len, key, sizeof(key)) < 0) ||
	    !uc_key_valid(key, APP_PARAM_KEY_MAX)) {
		*err = UC_ERR_INVALID;
		return NULL;
	}
	value = uc_app_cfg_param(&s->cfg, key);
	if (value == NULL) {
		*err = UC_ERR_NOT_FOUND;
	}

	return value;
}

static int32_t h_param_get(wasm_exec_env_t env, uint32_t key_off, uint32_t key_len,
			   uint32_t buf_off, uint32_t buf_len)
{
	struct uc_app_slot *s = slot_of(env);
	int32_t err = UC_OK;
	const char *value;
	char *dst = NULL;
	size_t vlen;

	if (buf_len > 0U) {
		dst = app_mem(env, buf_off, buf_len);
		if (dst == NULL) {
			return ret_api(s, UC_ERR_INVALID);
		}
	}
	value = param_lookup(env, key_off, key_len, &err);
	if (value == NULL) {
		return ret_lookup(s, err);
	}

	vlen = strlen(value);
	if (vlen > buf_len) {
		return ret_api(s, UC_ERR_INVALID);
	}
	if (vlen > 0U) {
		memcpy(dst, value, vlen);
	}
	if (vlen < buf_len) {
		dst[vlen] = '\0';
	}

	return (int32_t)vlen;
}

static int32_t h_param_get_number(wasm_exec_env_t env, uint32_t key_off, uint32_t key_len,
				  uint32_t out_off)
{
	struct uc_app_slot *s = slot_of(env);
	void *out = app_out_double(env, out_off);
	int32_t err = UC_OK;
	const char *value;
	char *end;
	double d;

	if (out == NULL) {
		return ret_api(s, UC_ERR_INVALID);
	}
	value = param_lookup(env, key_off, key_len, &err);
	if (value == NULL) {
		return ret_lookup(s, err);
	}

	d = strtod(value, &end);
	if (end == value) {
		return ret_api(s, UC_ERR_TYPE);
	}
	put_double(out, d);

	return UC_OK;
}

/* ---------------------------------------------------------------------- */
/* Local objects                                                           */
/* ---------------------------------------------------------------------- */

static int32_t h_obj_create(wasm_exec_env_t env, uint32_t type, uint32_t instance,
			    uint32_t name_off, uint32_t name_len)
{
	struct uc_app_slot *s = slot_of(env);
	char name[UC_NAME_MAX];

	if (!has_perm(s, UC_PERM_BACNET_LOCAL)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (!object_id_ok(type, instance) || (instance == BACNET_MAX_INSTANCE) ||
	    !uc_obj_type_supported((uint16_t)type)) {
		return ret_api(s, UC_ERR_INVALID);
	}
	name[0] = '\0';
	if ((name_len > 0U) && (app_str(env, name_off, name_len, name, sizeof(name)) < 0)) {
		return ret_api(s, UC_ERR_INVALID);
	}

	return ret_errno(s, uc_bn_obj_create((uint16_t)type, instance,
					     (name[0] != '\0') ? name : NULL, s->owner));
}

static int32_t h_obj_delete(wasm_exec_env_t env, uint32_t type, uint32_t instance)
{
	struct uc_app_slot *s = slot_of(env);

	if (!has_perm(s, UC_PERM_BACNET_LOCAL)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (!object_id_ok(type, instance)) {
		return ret_api(s, UC_ERR_INVALID);
	}

	return ret_errno(s, uc_bn_obj_delete((uint16_t)type, instance, s->owner));
}

static int32_t local_read(struct uc_app_slot *s, uint32_t type, uint32_t instance, uint32_t prop,
			  int32_t index, void *out)
{
	BACNET_APPLICATION_DATA_VALUE value;
	double d;
	int rc;

	memset(&value, 0, sizeof(value));
	rc = uc_bn_prop_read((uint16_t)type, instance, prop, index, &value);
	if (rc == 0) {
		rc = uc_value_to_double(&value, &d);
	}
	if (rc < 0) {
		return ret_errno(s, rc);
	}
	put_double(out, d);

	return UC_OK;
}

static int32_t h_prop_read(wasm_exec_env_t env, uint32_t type, uint32_t instance, uint32_t prop,
			   int32_t index, uint32_t out_off)
{
	struct uc_app_slot *s = slot_of(env);
	void *out = app_out_double(env, out_off);

	if (!has_perm(s, UC_PERM_BACNET_LOCAL)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if ((out == NULL) || !object_id_ok(type, instance)) {
		return ret_api(s, UC_ERR_INVALID);
	}

	return local_read(s, type, instance, prop, index, out);
}

/* NULL value: relinquish. */
static int32_t local_write(struct uc_app_slot *s, uint32_t type, uint32_t instance, uint32_t prop,
			   int32_t index, const BACNET_APPLICATION_DATA_VALUE *value,
			   uint32_t priority)
{
	if (!has_perm(s, UC_PERM_BACNET_LOCAL)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (!object_id_ok(type, instance) || (priority > BACNET_MAX_PRIORITY)) {
		return ret_api(s, UC_ERR_INVALID);
	}

	return ret_errno(s, uc_bn_prop_write((uint16_t)type, instance, prop, index, value,
					     (uint8_t)priority, s->owner));
}

static int32_t h_prop_write(wasm_exec_env_t env, uint32_t type, uint32_t instance, uint32_t prop,
			    int32_t index, double value, uint32_t priority)
{
	struct uc_app_slot *s = slot_of(env);
	BACNET_APPLICATION_DATA_VALUE v;
	int rc;

	if (!has_perm(s, UC_PERM_BACNET_LOCAL)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (!object_id_ok(type, instance)) {
		return ret_api(s, UC_ERR_INVALID);
	}
	rc = uc_value_from_double((uint16_t)type, prop, value, &v);
	if (rc < 0) {
		return ret_errno(s, rc);
	}

	return local_write(s, type, instance, prop, index, &v, priority);
}

static int32_t h_prop_write_null(wasm_exec_env_t env, uint32_t type, uint32_t instance,
				 uint32_t prop, uint32_t priority)
{
	return local_write(slot_of(env), type, instance, prop, UC_ARRAY_ALL, NULL, priority);
}

static int32_t h_prop_write_string(wasm_exec_env_t env, uint32_t type, uint32_t instance,
				   uint32_t prop, uint32_t str_off, uint32_t len)
{
	struct uc_app_slot *s = slot_of(env);
	BACNET_APPLICATION_DATA_VALUE v;
	const char *str = "";

	if (!has_perm(s, UC_PERM_BACNET_LOCAL)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (len > 0U) {
		str = app_mem(env, str_off, len);
		if ((str == NULL) || (memchr(str, '\0', len) != NULL)) {
			return ret_api(s, UC_ERR_INVALID);
		}
	}

	memset(&v, 0, sizeof(v));
	v.tag = BACNET_APPLICATION_TAG_CHARACTER_STRING;
	if (!characterstring_init(&v.type.Character_String, CHARACTER_UTF8, str, len)) {
		return ret_api(s, UC_ERR_INVALID);
	}

	return local_write(s, type, instance, prop, UC_ARRAY_ALL, &v, UC_PRIORITY_NONE);
}

/* ---------------------------------------------------------------------- */
/* Remote devices                                                          */
/* ---------------------------------------------------------------------- */

static int32_t h_remote_read(wasm_exec_env_t env, uint32_t device, uint32_t type,
			     uint32_t instance, uint32_t prop, int32_t index, uint32_t out_off,
			     uint32_t timeout_ms)
{
	struct uc_app_slot *s = slot_of(env);
	void *out = app_out_double(env, out_off);
	BACNET_APPLICATION_DATA_VALUE value;
	double d;
	int rc;

	if ((out == NULL) || !object_id_ok(type, instance)) {
		return ret_api(s, UC_ERR_INVALID);
	}
	if (device_is_local(device)) {
		/* the own device needs no request */
		if (!has_perm(s, UC_PERM_BACNET_LOCAL)) {
			return ret_api(s, UC_ERR_PERM);
		}
		return local_read(s, type, instance, prop, index, out);
	}
	if (!has_perm(s, UC_PERM_BACNET_REMOTE)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (device >= BACNET_MAX_INSTANCE) {
		return ret_api(s, UC_ERR_INVALID);
	}

	memset(&value, 0, sizeof(value));
	uc_app_wd_pause(s);
	rc = uc_bn_remote_read(device, (uint16_t)type, instance, prop, index, &value, timeout_ms);
	uc_app_wd_resume(s);
	if (rc == 0) {
		rc = uc_value_to_double(&value, &d);
	}
	if (rc < 0) {
		return ret_errno(s, rc);
	}
	put_double(out, d);

	return UC_OK;
}

/* value == NULL: relinquish */
static int32_t remote_write(struct uc_app_slot *s, uint32_t device, uint32_t type,
			    uint32_t instance, uint32_t prop, int32_t index,
			    const BACNET_APPLICATION_DATA_VALUE *value, uint32_t priority,
			    uint32_t timeout_ms)
{
	BACNET_APPLICATION_DATA_VALUE null_value;
	int rc;

	if (!object_id_ok(type, instance) || (priority > BACNET_MAX_PRIORITY)) {
		return ret_api(s, UC_ERR_INVALID);
	}
	if (device_is_local(device)) {
		return local_write(s, type, instance, prop, index, value, priority);
	}
	if (!has_perm(s, UC_PERM_BACNET_REMOTE)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (device >= BACNET_MAX_INSTANCE) {
		return ret_api(s, UC_ERR_INVALID);
	}
	if (value == NULL) {
		memset(&null_value, 0, sizeof(null_value));
		null_value.tag = BACNET_APPLICATION_TAG_NULL;
		value = &null_value;
	}

	uc_app_wd_pause(s);
	rc = uc_bn_remote_write(device, (uint16_t)type, instance, prop, index, value,
				(uint8_t)priority, timeout_ms);
	uc_app_wd_resume(s);

	return ret_errno(s, rc);
}

static int32_t h_remote_write(wasm_exec_env_t env, uint32_t device, uint32_t type,
			      uint32_t instance, uint32_t prop, int32_t index, double value,
			      uint32_t priority, uint32_t timeout_ms)
{
	struct uc_app_slot *s = slot_of(env);
	BACNET_APPLICATION_DATA_VALUE v;
	int rc;

	if (!object_id_ok(type, instance)) {
		return ret_api(s, UC_ERR_INVALID);
	}
	rc = uc_value_from_double((uint16_t)type, prop, value, &v);
	if (rc < 0) {
		return ret_errno(s, rc);
	}

	return remote_write(s, device, type, instance, prop, index, &v, priority, timeout_ms);
}

static int32_t h_remote_write_null(wasm_exec_env_t env, uint32_t device, uint32_t type,
				   uint32_t instance, uint32_t prop, uint32_t priority,
				   uint32_t timeout_ms)
{
	return remote_write(slot_of(env), device, type, instance, prop, UC_ARRAY_ALL, NULL,
			    priority, timeout_ms);
}

/* ---------------------------------------------------------------------- */
/* COV subscriptions                                                       */
/* ---------------------------------------------------------------------- */

/* BACnet thread, cov_lock held: queue only. */
static void app_cov_cb(void *ctx, int sub_id, uint32_t device, uint16_t type, uint32_t instance,
		       uint32_t prop, double value)
{
	struct uc_app_event ev = {0};

	ev.kind = UC_APP_EV_COV;
	ev.sub_id = sub_id;
	ev.device = device;
	ev.type = type;
	ev.instance = instance;
	ev.prop = prop;
	ev.value = value;
	(void)uc_app_post_event(ctx, &ev);
}

static int32_t h_cov_subscribe(wasm_exec_env_t env, uint32_t device, uint32_t type,
			       uint32_t instance, uint32_t lifetime_s)
{
	struct uc_app_slot *s = slot_of(env);
	struct uc_app_sub *sub = NULL;
	bool local = device_is_local(device);
	int id;

	if (!has_perm(s, local ? UC_PERM_BACNET_LOCAL : UC_PERM_BACNET_REMOTE)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (!object_id_ok(type, instance) || (!local && (device >= BACNET_MAX_INSTANCE))) {
		return ret_api(s, UC_ERR_INVALID);
	}
	for (size_t i = 0; i < ARRAY_SIZE(s->subs); i++) {
		if (s->subs[i].id < 0) {
			sub = &s->subs[i];
			break;
		}
	}
	if (sub == NULL) {
		return ret_api(s, UC_ERR_NO_MEM);
	}

	id = uc_bn_cov_subscribe(device, (uint16_t)type, instance, lifetime_s, app_cov_cb, s);
	if (id < 0) {
		return ret_errno(s, id);
	}
	sub->id = id;
	sub->device = device;
	sub->type = (uint16_t)type;
	sub->instance = instance;

	return id;
}

static int32_t h_cov_unsubscribe(wasm_exec_env_t env, int32_t sub_id)
{
	struct uc_app_slot *s = slot_of(env);

	if (sub_id < 0) {
		return ret_api(s, UC_ERR_INVALID);
	}
	/* only subscriptions of this instance */
	for (size_t i = 0; i < ARRAY_SIZE(s->subs); i++) {
		if (s->subs[i].id == sub_id) {
			s->subs[i].id = -1;
			return ret_errno(s, uc_bn_cov_unsubscribe(sub_id));
		}
	}

	return ret_api(s, UC_ERR_NOT_FOUND);
}

bool uc_app_host_sub_live(const struct uc_app_slot *s, const struct uc_app_event *ev)
{
	for (size_t i = 0; i < ARRAY_SIZE(s->subs); i++) {
		const struct uc_app_sub *sub = &s->subs[i];

		if ((sub->id >= 0) && (sub->id == ev->sub_id) && (sub->type == ev->type) &&
		    (sub->instance == ev->instance)) {
			return true;
		}
	}

	return false;
}

void uc_app_host_reset(struct uc_app_slot *s)
{
	for (size_t i = 0; i < ARRAY_SIZE(s->subs); i++) {
		s->subs[i].id = -1;
	}
	s->log_window_ms = 0;
	s->log_count = 0;
	s->log_dropped = 0;
}

void uc_app_host_cleanup(struct uc_app_slot *s)
{
	uc_bn_cov_unsubscribe_ctx(s);
	for (size_t i = 0; i < ARRAY_SIZE(s->subs); i++) {
		s->subs[i].id = -1;
	}
}

/* ---------------------------------------------------------------------- */
/* Raw IO channels                                                         */
/* ---------------------------------------------------------------------- */

static int32_t h_io_find(wasm_exec_env_t env, uint32_t name_off, uint32_t len)
{
	struct uc_app_slot *s = slot_of(env);
	char name[UC_CHANNEL_NAME_MAX];

	if (!has_perm(s, UC_PERM_IO)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if ((len == 0U) || (app_mem(env, name_off, len) == NULL)) {
		return ret_api(s, UC_ERR_INVALID);
	}
	if (len >= sizeof(name)) {
		/* longer than any catalog name */
		return ret_api(s, UC_ERR_NOT_FOUND);
	}
	if (app_str(env, name_off, len, name, sizeof(name)) < 0) {
		return ret_api(s, UC_ERR_INVALID);
	}

	return ret_errno(s, uc_io_find(name));
}

static int32_t h_io_read(wasm_exec_env_t env, int32_t channel, uint32_t out_off)
{
	struct uc_app_slot *s = slot_of(env);
	void *out = app_out_double(env, out_off);
	double d;
	int rc;

	if (!has_perm(s, UC_PERM_IO)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (out == NULL) {
		return ret_api(s, UC_ERR_INVALID);
	}
	rc = uc_io_read(channel, &d);
	if (rc < 0) {
		return ret_errno(s, rc);
	}
	put_double(out, d);

	return UC_OK;
}

static int32_t h_io_write(wasm_exec_env_t env, int32_t channel, double value)
{
	struct uc_app_slot *s = slot_of(env);

	if (!has_perm(s, UC_PERM_IO)) {
		return ret_api(s, UC_ERR_PERM);
	}

	return ret_errno(s, uc_io_write(channel, value));
}

/* ---------------------------------------------------------------------- */
/* Key/value store: /lfs/data/<app>/<key>                                  */
/* ---------------------------------------------------------------------- */

static int kv_key(wasm_exec_env_t env, uint32_t key_off, uint32_t key_len,
		  char key[APP_KV_KEY_MAX + 1])
{
	if ((app_str(env, key_off, key_len, key, APP_KV_KEY_MAX + 1) < 0) ||
	    !uc_key_valid(key, APP_KV_KEY_MAX)) {
		return -EINVAL;
	}

	return 0;
}

static int kv_path(char *buf, size_t size, const char *app, const char *key, const char *suffix)
{
	int n;

	if (key != NULL) {
		n = snprintk(buf, size, "%s/%s/%s%s", UC_DIR_DATA, app, key, suffix);
	} else {
		n = snprintk(buf, size, "%s/%s", UC_DIR_DATA, app);
	}

	return ((n < 0) || ((size_t)n >= size)) ? -ENAMETOOLONG : 0;
}

static int kv_read(const char *path, void *dst, size_t len)
{
	struct fs_file_t file;
	size_t done = 0;
	int rc;

	fs_file_t_init(&file);
	rc = fs_open(&file, path, FS_O_READ);
	if (rc < 0) {
		return rc;
	}
	while (done < len) {
		ssize_t n = fs_read(&file, (uint8_t *)dst + done, len - done);

		if (n <= 0) {
			rc = (n < 0) ? (int)n : -EIO;
			break;
		}
		done += (size_t)n;
	}
	(void)fs_close(&file);

	return rc;
}

/* Write <path>~, then rename over <path>: a reader sees the old or the
 * new value. (uc_storage_write_file() is limited to UC_PATH_MAX, which a
 * 23 character app name plus a 31 character key exceed.) */
static int kv_write(const char *path, const char *tmp, const void *src, size_t len)
{
	struct fs_file_t file;
	size_t done = 0;
	int rc;

	fs_file_t_init(&file);
	rc = fs_open(&file, tmp, FS_O_CREATE | FS_O_WRITE | FS_O_TRUNC);
	if (rc < 0) {
		return rc;
	}
	while (done < len) {
		ssize_t n = fs_write(&file, (const uint8_t *)src + done, len - done);

		if (n <= 0) {
			rc = (n < 0) ? (int)n : -ENOSPC;
			break;
		}
		done += (size_t)n;
	}
	if (fs_close(&file) < 0 && rc == 0) {
		rc = -EIO;
	}

	if (rc == 0) {
		rc = fs_rename(tmp, path);
		if (rc == -EEXIST) {
			(void)fs_unlink(path);
			rc = fs_rename(tmp, path);
		}
	}
	if (rc < 0) {
		(void)fs_unlink(tmp);
	}

	return rc;
}

static int32_t h_kv_get(wasm_exec_env_t env, uint32_t key_off, uint32_t key_len, uint32_t buf_off,
			uint32_t buf_len)
{
	struct uc_app_slot *s = slot_of(env);
	char path[APP_KV_PATH_MAX];
	char key[APP_KV_KEY_MAX + 1];
	void *dst = NULL;
	size_t size = 0;
	int rc;

	if (!has_perm(s, UC_PERM_KV)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if (kv_key(env, key_off, key_len, key) < 0) {
		return ret_api(s, UC_ERR_INVALID);
	}
	if (buf_len > 0U) {
		dst = app_mem(env, buf_off, buf_len);
		if (dst == NULL) {
			return ret_api(s, UC_ERR_INVALID);
		}
	}
	if (!uc_storage_ready()) {
		return ret_api(s, UC_ERR_IO);
	}
	rc = kv_path(path, sizeof(path), s->cfg.name, key, "");
	if (rc < 0) {
		return ret_errno(s, rc);
	}

	uc_app_wd_pause(s);
	rc = uc_storage_file_size(path, &size);
	if ((rc == 0) && (size > (size_t)INT32_MAX)) {
		rc = -EFBIG;
	}
	if ((rc == 0) && (buf_len > 0U) && (size > 0U)) {
		rc = kv_read(path, dst, MIN(size, (size_t)buf_len));
	}
	uc_app_wd_resume(s);

	if (rc == -EISDIR) {
		rc = -ENOENT;
	}
	if (rc == -ENOENT) {
		/* never stored: not counted as an error */
		return ret_lookup(s, (int32_t)uc_err_to_api(rc));
	}
	if (rc < 0) {
		return ret_errno(s, -EIO);
	}

	return (int32_t)size;
}

static int32_t h_kv_set(wasm_exec_env_t env, uint32_t key_off, uint32_t key_len, uint32_t val_off,
			uint32_t val_len)
{
	struct uc_app_slot *s = slot_of(env);
	char path[APP_KV_PATH_MAX];
	char tmp[APP_KV_PATH_MAX];
	char key[APP_KV_KEY_MAX + 1];
	const void *src = NULL;
	int rc;

	if (!has_perm(s, UC_PERM_KV)) {
		return ret_api(s, UC_ERR_PERM);
	}
	if ((kv_key(env, key_off, key_len, key) < 0) || (val_len > CONFIG_UC_APP_KV_VALUE_MAX)) {
		return ret_api(s, UC_ERR_INVALID);
	}
	if (val_len > 0U) {
		src = app_mem(env, val_off, val_len);
		if (src == NULL) {
			return ret_api(s, UC_ERR_INVALID);
		}
	}
	if (!uc_storage_ready()) {
		return ret_api(s, UC_ERR_IO);
	}
	rc = kv_path(path, sizeof(path), s->cfg.name, NULL, "");
	if (rc == 0) {
		size_t size;

		uc_app_wd_pause(s);
		/* -EISDIR: the directory exists (fs_mkdir() would log an error) */
		rc = uc_storage_file_size(path, &size);
		if (rc == -ENOENT) {
			rc = uc_storage_mkdir(path);
		} else if (rc == -EISDIR) {
			rc = 0;
		} else if (rc == 0) {
			rc = -ENOTDIR;
		}
		if (rc == 0) {
			rc = kv_path(path, sizeof(path), s->cfg.name, key, "");
		}
		if (rc == 0) {
			rc = kv_path(tmp, sizeof(tmp), s->cfg.name, key, APP_KV_TMP_SUFFIX);
		}
		if (rc == 0) {
			rc = kv_write(path, tmp, src, val_len);
		}
		uc_app_wd_resume(s);
	}
	if (rc < 0) {
		LOG_WRN("%s: kv_set %s failed (%d)", s->cfg.name, key, rc);
		return ret_errno(s, (rc == -ENAMETOOLONG) ? -EINVAL : -EIO);
	}

	return UC_OK;
}

int uc_app_kv_remove_all(const char *app_name)
{
	char dir[APP_KV_PATH_MAX];
	char path[APP_KV_PATH_MAX];
	struct fs_dirent *ent;
	struct fs_dir_t d;
	int first_err = 0;
	int rc;

	if ((app_name == NULL) || !uc_app_name_valid(app_name)) {
		return -EINVAL;
	}
	rc = kv_path(dir, sizeof(dir), app_name, NULL, "");
	if (rc < 0) {
		return rc;
	}

	ent = k_malloc(sizeof(*ent));
	if (ent == NULL) {
		return -ENOMEM;
	}

	/* Delete the first entry until the directory is empty (no unlink
	 * while a directory is being iterated). */
	for (int i = 0; i < APP_KV_REMOVE_MAX; i++) {
		fs_dir_t_init(&d);
		rc = fs_opendir(&d, dir);
		if (rc < 0) {
			break;
		}
		rc = fs_readdir(&d, ent);
		(void)fs_closedir(&d);
		if ((rc < 0) || (ent->name[0] == '\0')) {
			break;
		}
		if (snprintk(path, sizeof(path), "%s/%s", dir, ent->name) >= (int)sizeof(path)) {
			rc = -ENAMETOOLONG;
			break;
		}
		rc = fs_unlink(path);
		if (rc < 0) {
			break;
		}
	}
	k_free(ent);
	if (rc == -ENOENT) {
		/* no data directory */
		return 0;
	}
	if (rc < 0) {
		first_err = rc;
	}

	rc = uc_storage_remove(dir);
	if ((rc < 0) && (first_err == 0)) {
		first_err = rc;
	}

	return first_err;
}

/* ---------------------------------------------------------------------- */
/* Native symbol table                                                     */
/* ---------------------------------------------------------------------- */

/* Signatures: i = i32 (also pointers, checked by the host), I = i64,
 * F = f64. WAMR sorts the table in place, so it is not const. */
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

NativeSymbol *uc_app_host_natives(uint32_t *count)
{
	*count = ARRAY_SIZE(natives);
	return natives;
}

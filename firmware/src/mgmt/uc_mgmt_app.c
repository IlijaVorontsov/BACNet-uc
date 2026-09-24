/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * SMP group 64 "uc_app": WebAssembly applications (list, install, start,
 * stop, remove, status). See docs/management-protocol.md.
 *
 * Without CONFIG_UC_APPS every command answers rc UNSUPPORTED.
 */

#include <ctype.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include <zcbor_common.h>
#include <zcbor_decode.h>
#include <zcbor_encode.h>

#include "uc/uc_storage.h"

#include "uc_mgmt_util.h"

LOG_MODULE_REGISTER(uc_mgmt_app, CONFIG_UC_LOG_LEVEL);

#if defined(CONFIG_UC_APPS)

/* apps.schema.json limits */
#define APP_PERIOD_MS_MAX 3600000U
#define APP_HEAP_KB_MAX   256U
#define APP_STACK_KB_MIN  1U
#define APP_STACK_KB_MAX  64U
#define APP_FILE_BASE_MAX 40U

/* Longest permission name ("bacnet.remote") + NUL, with margin. */
#define PERM_NAME_MAX 16

static int app_errno(struct smp_streamer *ctxt, int err)
{
	return uc_mgmt_errno(ctxt, UC_MGMT_GROUP_APP, err);
}

/* ^/lfs/apps/[A-Za-z0-9_.-]{1,40}\.(wasm|aot)$ (apps.schema.json) */
static bool app_file_valid(const char *path)
{
	static const char prefix[] = UC_DIR_APPS "/";
	const char *base;
	const char *ext;

	if (strncmp(path, prefix, sizeof(prefix) - 1) != 0) {
		return false;
	}
	base = path + sizeof(prefix) - 1;
	ext = strrchr(base, '.');
	if ((ext == NULL) || ((strcmp(ext, ".wasm") != 0) && (strcmp(ext, ".aot") != 0))) {
		return false;
	}
	if ((ext == base) || ((size_t)(ext - base) > APP_FILE_BASE_MAX)) {
		return false;
	}
	for (const char *p = base; p < ext; p++) {
		if (!isalnum((unsigned char)*p) && (*p != '_') && (*p != '.') && (*p != '-')) {
			return false;
		}
	}

	return true;
}

/* ---------------------------------------------------------------------- */
/* Manifest decoders                                                       */
/* ---------------------------------------------------------------------- */

/* "perms": [tstr] -> UC_PERM_* bitmask */
static bool dec_perms(zcbor_state_t *zsd, uint32_t *perms)
{
	uint32_t result = 0;

	if (!zcbor_list_start_decode(zsd)) {
		return uc_mgmt_dec_fail(-EINVAL);
	}
	while (!zcbor_array_at_end(zsd)) {
		struct zcbor_string s;
		char name[PERM_NAME_MAX];
		uint32_t bit;

		if (!zcbor_tstr_decode(zsd, &s) || (s.len >= sizeof(name))) {
			return uc_mgmt_dec_fail(-EINVAL);
		}
		memcpy(name, s.value, s.len);
		name[s.len] = '\0';
		bit = uc_perm_from_str(name);
		if (bit == 0U) {
			return uc_mgmt_dec_fail(-EINVAL);
		}
		result |= bit;
	}
	if (!zcbor_list_end_decode(zsd)) {
		return uc_mgmt_dec_fail(-EINVAL);
	}
	*perms = result;

	return true;
}

/* "params": {tstr: tstr} -> cfg->params */
static bool dec_params(zcbor_state_t *zsd, struct uc_app_cfg *cfg)
{
	if (!zcbor_map_start_decode(zsd)) {
		return uc_mgmt_dec_fail(-EINVAL);
	}
	cfg->param_count = 0;
	while (!zcbor_array_at_end(zsd)) {
		struct uc_app_param *p;
		struct uc_mgmt_tstr key;
		struct uc_mgmt_tstr value;

		if (cfg->param_count >= ARRAY_SIZE(cfg->params)) {
			return uc_mgmt_dec_fail(-ENOSPC);
		}
		p = &cfg->params[cfg->param_count];
		key = (struct uc_mgmt_tstr){ .buf = p->key, .size = sizeof(p->key) };
		value = (struct uc_mgmt_tstr){ .buf = p->value, .size = sizeof(p->value) };
		if (!uc_mgmt_dec_tstr(zsd, &key) || !uc_mgmt_dec_tstr(zsd, &value)) {
			return false;
		}
		if (!uc_key_valid(p->key, sizeof(p->key) - 1) ||
		    (uc_app_cfg_param(cfg, p->key) != NULL)) {
			/* invalid or duplicate key */
			return uc_mgmt_dec_fail(-EINVAL);
		}
		cfg->param_count++;
	}
	if (!zcbor_map_end_decode(zsd)) {
		return uc_mgmt_dec_fail(-EINVAL);
	}

	return true;
}

/* "sha256": bstr of 32 bytes */
static bool dec_sha256(zcbor_state_t *zsd, struct uc_app_cfg *cfg)
{
	struct zcbor_string s;

	if (!zcbor_bstr_decode(zsd, &s) || (s.len != sizeof(cfg->sha256))) {
		return uc_mgmt_dec_fail(-EINVAL);
	}
	memcpy(cfg->sha256, s.value, sizeof(cfg->sha256));
	cfg->has_sha256 = true;

	return true;
}

/* {"name": tstr} plus optional extra keys; returns 0 or a negative errno */
static int dec_name_req(struct smp_streamer *ctxt, char *name, size_t size, bool *delete_file)
{
	struct uc_mgmt_tstr name_arg = { .buf = name, .size = size };
	bool del = false;
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("name", uc_mgmt_dec_tstr, &name_arg),
		ZCBOR_MAP_DECODE_KEY_DECODER("delete_file", zcbor_bool_decode, &del),
	};
	/* "delete_file" is only decoded for remove */
	size_t n = (delete_file != NULL) ? ARRAY_SIZE(req) : 1;
	int rc;

	name[0] = '\0';
	rc = uc_mgmt_decode(ctxt, req, n);
	if (rc < 0) {
		return rc;
	}
	if (!uc_mgmt_found(req, n, "name") || !uc_app_name_valid(name)) {
		return -EINVAL;
	}
	if (delete_file != NULL) {
		*delete_file = del;
	}

	return 0;
}

/* ---------------------------------------------------------------------- */
/* Status encoding                                                         */
/* ---------------------------------------------------------------------- */

static bool put_perms(zcbor_state_t *zse, uint32_t perms)
{
	bool ok = zcbor_list_start_encode(zse, 32);

	for (unsigned int i = 0; ok && (i < 32U); i++) {
		const char *name;

		if ((perms & BIT(i)) == 0U) {
			continue;
		}
		name = uc_perm_to_str(BIT(i));
		if (name != NULL) {
			ok = zcbor_tstr_put_term(zse, name, PERM_NAME_MAX);
		}
	}

	return ok && zcbor_list_end_encode(zse, 32);
}

/* The <app status> fields, into the currently open map. */
static bool put_status_fields(zcbor_state_t *zse, const struct uc_app_status *st)
{
	const struct uc_app_cfg *c = &st->cfg;

	return zcbor_tstr_put_lit(zse, "name") &&
	       uc_mgmt_put_tstr(zse, c->name, sizeof(c->name) - 1) &&
	       zcbor_tstr_put_lit(zse, "file") &&
	       uc_mgmt_put_tstr(zse, c->file, sizeof(c->file) - 1) &&
	       zcbor_tstr_put_lit(zse, "state") &&
	       uc_mgmt_put_tstr(zse, uc_app_state_str(st->state), 16) &&
	       zcbor_tstr_put_lit(zse, "autostart") && zcbor_bool_put(zse, c->autostart) &&
	       zcbor_tstr_put_lit(zse, "period_ms") && zcbor_uint32_put(zse, c->period_ms) &&
	       zcbor_tstr_put_lit(zse, "heap_kb") && zcbor_uint32_put(zse, c->heap_kb) &&
	       zcbor_tstr_put_lit(zse, "stack_kb") && zcbor_uint32_put(zse, c->stack_kb) &&
	       zcbor_tstr_put_lit(zse, "perms") && put_perms(zse, c->perms) &&
	       zcbor_tstr_put_lit(zse, "ticks") && zcbor_uint32_put(zse, st->ticks) &&
	       zcbor_tstr_put_lit(zse, "events") && zcbor_uint32_put(zse, st->events) &&
	       zcbor_tstr_put_lit(zse, "errors") && zcbor_uint32_put(zse, st->errors) &&
	       zcbor_tstr_put_lit(zse, "last_error") &&
	       uc_mgmt_put_tstr(zse, st->last_error, sizeof(st->last_error) - 1) &&
	       zcbor_tstr_put_lit(zse, "uptime_ms") &&
	       zcbor_uint64_put(zse, (st->state == UC_APP_RUNNING) ? st->uptime_ms : 0U);
}

/* ---------------------------------------------------------------------- */
/* Handlers                                                                */
/* ---------------------------------------------------------------------- */

/* list: {"offset"?: uint, "count"?: uint} -> {"total": uint, "apps": [...]}
 * offset/count/total extend the documented {} -> {"apps": [...]}: the list
 * holds as many entries as fit into one response.
 */
static int app_list(struct smp_streamer *ctxt)
{
	zcbor_state_t *zse = ctxt->writer->zs;
	struct uc_app_status *st = &uc_mgmt_scratch.app_status;
	uint32_t offset = 0;
	uint32_t count = UINT32_MAX;
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("offset", zcbor_uint32_decode, &offset),
		ZCBOR_MAP_DECODE_KEY_DECODER("count", zcbor_uint32_decode, &count),
	};
	size_t total;
	uint32_t n = 0;
	bool ok;
	int rc;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	if (rc < 0) {
		return app_errno(ctxt, rc);
	}

	total = uc_apps_installed();
	ok = zcbor_tstr_put_lit(zse, "total") && zcbor_uint32_put(zse, (uint32_t)total) &&
	     zcbor_tstr_put_lit(zse, "apps") && zcbor_list_start_encode(zse, CONFIG_UC_APPS_MAX);

	for (size_t i = offset; ok && (i < total) && (n < count); i++) {
		struct uc_mgmt_mark m;
		bool elem_ok;

		if (uc_apps_status(i, st) < 0) {
			/* removed concurrently */
			continue;
		}
		uc_mgmt_mark_set(zse, &m);
		elem_ok = zcbor_map_start_encode(zse, 14) && put_status_fields(zse, st) &&
			  zcbor_map_end_encode(zse, 14);
		if (!uc_mgmt_elem_commit(ctxt, &m, elem_ok)) {
			LOG_DBG("list: %u of %u apps fit", n, (unsigned int)total);
			break;
		}
		n++;
	}

	ok = ok && zcbor_list_end_encode(zse, CONFIG_UC_APPS_MAX);

	return MGMT_RETURN_CHECK(ok);
}

static int app_install(struct smp_streamer *ctxt)
{
	struct uc_app_cfg *cfg = &uc_mgmt_scratch.app_cfg;
	struct uc_mgmt_tstr name = { .buf = cfg->name, .size = sizeof(cfg->name) };
	struct uc_mgmt_tstr file = { .buf = cfg->file, .size = sizeof(cfg->file) };
	uint32_t period_ms;
	uint32_t heap_kb;
	uint32_t stack_kb;
	bool restart = false;
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("name", uc_mgmt_dec_tstr, &name),
		ZCBOR_MAP_DECODE_KEY_DECODER("file", uc_mgmt_dec_tstr, &file),
		ZCBOR_MAP_DECODE_KEY_DECODER("autostart", zcbor_bool_decode, &cfg->autostart),
		ZCBOR_MAP_DECODE_KEY_DECODER("period_ms", zcbor_uint32_decode, &period_ms),
		ZCBOR_MAP_DECODE_KEY_DECODER("heap_kb", zcbor_uint32_decode, &heap_kb),
		ZCBOR_MAP_DECODE_KEY_DECODER("stack_kb", zcbor_uint32_decode, &stack_kb),
		ZCBOR_MAP_DECODE_KEY_DECODER("perms", dec_perms, &cfg->perms),
		ZCBOR_MAP_DECODE_KEY_DECODER("params", dec_params, cfg),
		ZCBOR_MAP_DECODE_KEY_DECODER("sha256", dec_sha256, cfg),
		ZCBOR_MAP_DECODE_KEY_DECODER("restart", zcbor_bool_decode, &restart),
	};
	int rc;

	uc_config_app_defaults(cfg);
	period_ms = cfg->period_ms;
	heap_kb = cfg->heap_kb;
	stack_kb = cfg->stack_kb;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	if (rc < 0) {
		LOG_WRN("install: malformed manifest (%d)", rc);
		return app_errno(ctxt, rc);
	}
	if (!uc_mgmt_found(req, ARRAY_SIZE(req), "name") ||
	    !uc_mgmt_found(req, ARRAY_SIZE(req), "file") || !uc_app_name_valid(cfg->name) ||
	    !app_file_valid(cfg->file) || (period_ms > APP_PERIOD_MS_MAX) ||
	    (heap_kb > APP_HEAP_KB_MAX) || (stack_kb < APP_STACK_KB_MIN) ||
	    (stack_kb > APP_STACK_KB_MAX)) {
		LOG_WRN("install: invalid manifest for '%s'", cfg->name);
		return app_errno(ctxt, -EINVAL);
	}
	cfg->period_ms = period_ms;
	cfg->heap_kb = (uint16_t)heap_kb;
	cfg->stack_kb = (uint16_t)stack_kb;

	rc = uc_apps_install(cfg, restart);
	if (rc < 0) {
		LOG_WRN("install %s (%s): %d (%s)", cfg->name, cfg->file, rc, uc_err_str(rc));
	} else {
		LOG_INF("installed %s (%s)%s", cfg->name, cfg->file,
			restart ? ", restart requested" : "");
	}

	return app_errno(ctxt, rc);
}

static int app_start(struct smp_streamer *ctxt)
{
	char name[UC_APP_NAME_MAX];
	int rc = dec_name_req(ctxt, name, sizeof(name), NULL);

	if (rc == 0) {
		rc = uc_apps_start(name);
		LOG_INF("start %s: %d", name, rc);
	}

	return app_errno(ctxt, rc);
}

static int app_stop(struct smp_streamer *ctxt)
{
	char name[UC_APP_NAME_MAX];
	int rc = dec_name_req(ctxt, name, sizeof(name), NULL);

	if (rc == 0) {
		rc = uc_apps_stop(name);
		LOG_INF("stop %s: %d", name, rc);
	}

	return app_errno(ctxt, rc);
}

static int app_remove(struct smp_streamer *ctxt)
{
	char name[UC_APP_NAME_MAX];
	bool delete_file = false;
	int rc = dec_name_req(ctxt, name, sizeof(name), &delete_file);

	if (rc == 0) {
		rc = uc_apps_remove(name, delete_file);
		LOG_INF("remove %s%s: %d", name, delete_file ? " (delete file)" : "", rc);
	}

	return app_errno(ctxt, rc);
}

static int app_status(struct smp_streamer *ctxt)
{
	struct uc_app_status *st = &uc_mgmt_scratch.app_status;
	char name[UC_APP_NAME_MAX];
	int rc = dec_name_req(ctxt, name, sizeof(name), NULL);

	if (rc == 0) {
		rc = uc_apps_status_by_name(name, st);
	}
	if (rc < 0) {
		return app_errno(ctxt, rc);
	}

	return MGMT_RETURN_CHECK(put_status_fields(ctxt->writer->zs, st));
}

static const struct mgmt_handler uc_app_handlers[] = {
	[UC_MGMT_APP_LIST] = { .mh_read = app_list },
	[UC_MGMT_APP_INSTALL] = { .mh_write = app_install },
	[UC_MGMT_APP_START] = { .mh_write = app_start },
	[UC_MGMT_APP_STOP] = { .mh_write = app_stop },
	[UC_MGMT_APP_REMOVE] = { .mh_write = app_remove },
	[UC_MGMT_APP_STATUS] = { .mh_read = app_status },
};

#else /* !CONFIG_UC_APPS */

static int app_unsupported(struct smp_streamer *ctxt)
{
	return uc_mgmt_rc(ctxt, UC_MGMT_GROUP_APP, UC_MGMT_RC_UNSUPPORTED);
}

static const struct mgmt_handler uc_app_handlers[] = {
	[UC_MGMT_APP_LIST] = { .mh_read = app_unsupported },
	[UC_MGMT_APP_INSTALL] = { .mh_write = app_unsupported },
	[UC_MGMT_APP_START] = { .mh_write = app_unsupported },
	[UC_MGMT_APP_STOP] = { .mh_write = app_unsupported },
	[UC_MGMT_APP_REMOVE] = { .mh_write = app_unsupported },
	[UC_MGMT_APP_STATUS] = { .mh_read = app_unsupported },
};

#endif /* CONFIG_UC_APPS */

static struct mgmt_group uc_app_group = {
	.mg_handlers = uc_app_handlers,
	.mg_handlers_count = ARRAY_SIZE(uc_app_handlers),
	.mg_group_id = UC_MGMT_GROUP_APP,
#if defined(CONFIG_MCUMGR_SMP_SUPPORT_ORIGINAL_PROTOCOL)
	.mg_translate_error = uc_mgmt_translate_error,
#endif
#if defined(CONFIG_MCUMGR_GRP_ENUM_DETAILS_NAME)
	.mg_group_name = "uc_app",
#endif
};

void uc_mgmt_app_register(void)
{
	mgmt_register_group(&uc_app_group);
}

/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * SMP group 65 "uc_io": IO channel catalog, read, write and force/release.
 * See docs/management-protocol.md.
 */


#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include <zcbor_common.h>
#include <zcbor_decode.h>
#include <zcbor_encode.h>

#include "uc/uc_io.h"

#include "uc_mgmt_util.h"

LOG_MODULE_REGISTER(uc_mgmt_io, CONFIG_UC_LOG_LEVEL);

/* Longest kind/hw text ("gpio", "sim", ...) + NUL, with margin. */
#define ENUM_TEXT_MAX 8

static int io_errno(struct smp_streamer *ctxt, int err)
{
	return uc_mgmt_errno(ctxt, UC_MGMT_GROUP_IO, err);
}

/* Channel id of a request name, or a negative errno. */
static int io_channel_id(const char *name)
{
	int id = uc_io_find(name);

	return (id < 0) ? -ENOENT : id;
}

static bool put_channel(zcbor_state_t *zse, const struct uc_io_channel_info *ci)
{
	bool ok = zcbor_map_start_encode(zse, 7) &&
		  zcbor_tstr_put_lit(zse, "id") && zcbor_uint32_put(zse, (uint32_t)ci->id) &&
		  zcbor_tstr_put_lit(zse, "name") &&
		  uc_mgmt_put_tstr(zse, ci->name, UC_CHANNEL_NAME_MAX - 1) &&
		  zcbor_tstr_put_lit(zse, "desc") &&
		  uc_mgmt_put_tstr(zse, ci->desc, UC_NAME_MAX - 1) &&
		  zcbor_tstr_put_lit(zse, "kind") &&
		  uc_mgmt_put_tstr(zse, uc_io_kind_str(ci->kind), ENUM_TEXT_MAX) &&
		  zcbor_tstr_put_lit(zse, "hw") &&
		  uc_mgmt_put_tstr(zse, uc_io_hw_str(ci->hw), ENUM_TEXT_MAX) &&
		  zcbor_tstr_put_lit(zse, "forced") && zcbor_bool_put(zse, ci->forced);

	if (ok && ci->bound) {
		ok = zcbor_tstr_put_lit(zse, "object") && zcbor_map_start_encode(zse, 2) &&
		     zcbor_tstr_put_lit(zse, "type") && uc_mgmt_put_obj_type(zse, ci->obj_type) &&
		     zcbor_tstr_put_lit(zse, "instance") &&
		     zcbor_uint32_put(zse, ci->obj_instance) && zcbor_map_end_encode(zse, 2);
	}

	return ok && zcbor_map_end_encode(zse, 7);
}

/* catalog: {"offset"?: uint, "count"?: uint} ->
 *          {"board": tstr, "total": uint, "channels": [<channel>...]}
 * offset/count/total extend the documented {} -> {"board", "channels"}:
 * the list holds as many channels as fit into one response.
 */
static int io_catalog(struct smp_streamer *ctxt)
{
	zcbor_state_t *zse = ctxt->writer->zs;
	uint32_t offset = 0;
	uint32_t count = UINT32_MAX;
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("offset", zcbor_uint32_decode, &offset),
		ZCBOR_MAP_DECODE_KEY_DECODER("count", zcbor_uint32_decode, &count),
	};
	size_t total = uc_io_channel_count();
	uint32_t n = 0;
	bool ok;
	int rc;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	if (rc < 0) {
		return io_errno(ctxt, rc);
	}

	ok = zcbor_tstr_put_lit(zse, "board") &&
	     uc_mgmt_put_tstr(zse, uc_io_board_name(), UC_NAME_MAX - 1) &&
	     zcbor_tstr_put_lit(zse, "total") && zcbor_uint32_put(zse, (uint32_t)total) &&
	     zcbor_tstr_put_lit(zse, "channels") && zcbor_list_start_encode(zse, total);

	for (size_t id = offset; ok && (id < total) && (n < count); id++) {
		struct uc_io_channel_info ci;
		struct uc_mgmt_mark m;

		if (uc_io_channel_info((int)id, &ci) < 0) {
			continue;
		}
		uc_mgmt_mark_set(zse, &m);
		if (!uc_mgmt_elem_commit(ctxt, &m, put_channel(zse, &ci))) {
			LOG_DBG("catalog: %u of %u channels fit", (unsigned int)n,
				(unsigned int)total);
			break;
		}
		n++;
	}

	ok = ok && zcbor_list_end_encode(zse, total);

	return MGMT_RETURN_CHECK(ok);
}

/* read: {"name"?: tstr} -> {"values": {tstr: float}} */
static int io_read(struct smp_streamer *ctxt)
{
	zcbor_state_t *zse = ctxt->writer->zs;
	char name[UC_CHANNEL_NAME_MAX];
	struct uc_mgmt_tstr name_arg = { .buf = name, .size = sizeof(name) };
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("name", uc_mgmt_dec_tstr, &name_arg),
	};
	size_t total = uc_io_channel_count();
	bool ok;
	double v;
	int rc;
	int id;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	if (rc < 0) {
		return io_errno(ctxt, rc);
	}

	if (uc_mgmt_found(req, ARRAY_SIZE(req), "name")) {
		id = io_channel_id(name);
		rc = (id < 0) ? id : uc_io_read(id, &v);
		if (rc < 0) {
			return io_errno(ctxt, rc);
		}
		ok = zcbor_tstr_put_lit(zse, "values") && zcbor_map_start_encode(zse, 1) &&
		     uc_mgmt_put_tstr(zse, name, UC_CHANNEL_NAME_MAX - 1) &&
		     uc_mgmt_put_float(zse, v) && zcbor_map_end_encode(zse, 1);
		return MGMT_RETURN_CHECK(ok);
	}

	ok = zcbor_tstr_put_lit(zse, "values") && zcbor_map_start_encode(zse, total);
	for (size_t i = 0; ok && (i < total); i++) {
		struct uc_io_channel_info ci;
		struct uc_mgmt_mark m;

		if ((uc_io_channel_info((int)i, &ci) < 0) || (uc_io_read((int)i, &v) < 0)) {
			/* channel without working hardware: left out */
			continue;
		}
		uc_mgmt_mark_set(zse, &m);
		if (!uc_mgmt_elem_commit(ctxt, &m,
					 uc_mgmt_put_tstr(zse, ci.name, UC_CHANNEL_NAME_MAX - 1) &&
						 uc_mgmt_put_float(zse, v))) {
			LOG_WRN("read: values truncated at channel %u", (unsigned int)i);
			break;
		}
	}
	ok = ok && zcbor_map_end_encode(zse, total);

	return MGMT_RETURN_CHECK(ok);
}

/* write: {"name": tstr, "value": float} -> {} */
static int io_write(struct smp_streamer *ctxt)
{
	char name[UC_CHANNEL_NAME_MAX];
	struct uc_mgmt_tstr name_arg = { .buf = name, .size = sizeof(name) };
	double value = 0.0;
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("name", uc_mgmt_dec_tstr, &name_arg),
		ZCBOR_MAP_DECODE_KEY_DECODER("value", uc_mgmt_dec_double, &value),
	};
	int rc;
	int id;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	if ((rc == 0) && (!uc_mgmt_found(req, ARRAY_SIZE(req), "name") ||
			  !uc_mgmt_found(req, ARRAY_SIZE(req), "value"))) {
		rc = -EINVAL;
	}
	if (rc < 0) {
		return io_errno(ctxt, rc);
	}

	id = io_channel_id(name);
	rc = (id < 0) ? id : uc_io_write(id, value);
	LOG_INF("write %s = %g: %d", name, value, rc);

	return io_errno(ctxt, rc);
}

/* force: {"name": tstr, "value": float} or {"name": tstr, "release": true}
 * -> {}
 */
static int io_force(struct smp_streamer *ctxt)
{
	char name[UC_CHANNEL_NAME_MAX];
	struct uc_mgmt_tstr name_arg = { .buf = name, .size = sizeof(name) };
	double value = 0.0;
	bool release = false;
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("name", uc_mgmt_dec_tstr, &name_arg),
		ZCBOR_MAP_DECODE_KEY_DECODER("value", uc_mgmt_dec_double, &value),
		ZCBOR_MAP_DECODE_KEY_DECODER("release", zcbor_bool_decode, &release),
	};
	bool has_value;
	int rc;
	int id;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	has_value = uc_mgmt_found(req, ARRAY_SIZE(req), "value");
	/* exactly one of "value" and "release": true */
	if ((rc == 0) && (!uc_mgmt_found(req, ARRAY_SIZE(req), "name") ||
			  (release == has_value))) {
		rc = -EINVAL;
	}
	if (rc < 0) {
		return io_errno(ctxt, rc);
	}

	id = io_channel_id(name);
	if (id < 0) {
		rc = id;
	} else if (release) {
		rc = uc_io_release(id);
		LOG_INF("release %s: %d", name, rc);
	} else {
		rc = uc_io_force(id, value);
		LOG_INF("force %s = %g: %d", name, value, rc);
	}

	return io_errno(ctxt, rc);
}

static const struct mgmt_handler uc_io_handlers[] = {
	[UC_MGMT_IO_CATALOG] = { .mh_read = io_catalog },
	[UC_MGMT_IO_READ] = { .mh_read = io_read },
	[UC_MGMT_IO_WRITE] = { .mh_write = io_write },
	[UC_MGMT_IO_FORCE] = { .mh_write = io_force },
};

static struct mgmt_group uc_io_group = {
	.mg_handlers = uc_io_handlers,
	.mg_handlers_count = ARRAY_SIZE(uc_io_handlers),
	.mg_group_id = UC_MGMT_GROUP_IO,
#if defined(CONFIG_MCUMGR_SMP_SUPPORT_ORIGINAL_PROTOCOL)
	.mg_translate_error = uc_mgmt_translate_error,
#endif
#if defined(CONFIG_MCUMGR_GRP_ENUM_DETAILS_NAME)
	.mg_group_name = "uc_io",
#endif
};

void uc_mgmt_io_register(void)
{
	mgmt_register_group(&uc_io_group);
}

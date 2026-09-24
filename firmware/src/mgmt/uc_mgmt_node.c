/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * SMP group 66 "uc_node": node information, configuration reload, local
 * BACnet object listing and property access. See
 * docs/management-protocol.md.
 */

/*
 * The guest ABI header provides UC_API_VERSION. Its guest-side prototypes
 * uc_io_find/uc_io_read/uc_io_write collide with the firmware's uc_io.h, so
 * they are renamed while it is included (only the macros are used here).
 */
#define uc_io_find  uc_guest_io_find
#define uc_io_read  uc_guest_io_read
#define uc_io_write uc_guest_io_write
#include "../../../wasm/sdk/include/bacnet_uc.h"
#undef uc_io_find
#undef uc_io_read
#undef uc_io_write

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/printk.h>

#include <zcbor_common.h>
#include <zcbor_decode.h>
#include <zcbor_encode.h>

#include "uc/uc_storage.h"
#include "uc/uc_net.h"
#include "uc/uc_io.h"
#if defined(CONFIG_UC_APPS)
/* uc_apps_owner_name(): owner id -> app name, proposed for uc_apps.h */
#include "apps/uc_app_internal.h"
#endif

#include "uc_mgmt_util.h"

LOG_MODULE_REGISTER(uc_mgmt_node, CONFIG_UC_LOG_LEVEL);

/* objects: default page size. A typical entry (analog-value, 20 character
 * name, io owner, REAL pv) takes ~75 bytes, so 8 entries leave room for
 * long names; larger requested counts are cut where the response is full.
 */
#define OBJECTS_COUNT_DEFAULT 8

/* "app:" + app name + NUL */
#define OWNER_TEXT_MAX (4 + UC_APP_NAME_MAX)

/* Longest "doc" value of reload ("device") + NUL, with margin. */
#define DOC_TEXT_MAX 8

static int node_errno(struct smp_streamer *ctxt, int err)
{
	return uc_mgmt_errno(ctxt, UC_MGMT_GROUP_NODE, err);
}

/* ---------------------------------------------------------------------- */
/* info                                                                    */
/* ---------------------------------------------------------------------- */

static int node_info(struct smp_streamer *ctxt)
{
	zcbor_state_t *zse = ctxt->writer->zs;
	struct uc_bn_status st;
	struct uc_wasm_info wi = { .aot_target = "" };
	uint64_t fs_total = 0;
	uint64_t fs_free = 0;
	bool fs_ready = uc_storage_ready();
	uint32_t installed = 0;
	uint32_t running = 0;
	bool ok;

	uc_bn_status_get(&st);
	if ((st.device_name[0] == '\0') || (st.udp_port == 0U)) {
		/* the BACnet thread has not published its status yet */
		struct uc_device_cfg *dev = &uc_mgmt_scratch.device_cfg;

		uc_config_get_device(dev);
		if (st.device_name[0] == '\0') {
			st.device_instance = dev->instance;
			(void)uc_strlcpy(st.device_name, dev->name, sizeof(st.device_name));
		}
		if (st.udp_port == 0U) {
			st.udp_port = dev->udp_port;
		}
	}
	if (st.ipv4[0] == '\0') {
		uc_net_ipv4_str(st.ipv4, sizeof(st.ipv4));
	}
	if (fs_ready && (uc_storage_stats(&fs_total, &fs_free) < 0)) {
		fs_total = 0;
		fs_free = 0;
	}
#if defined(CONFIG_UC_APPS)
	installed = (uint32_t)uc_apps_installed();
	running = (uint32_t)uc_apps_running();
	uc_apps_wasm_info(&wi);
#endif

	ok = zcbor_tstr_put_lit(zse, "fw") &&
	     uc_mgmt_put_tstr(zse, CONFIG_UC_FW_VERSION, UC_MGMT_TSTR_MAX) &&
	     zcbor_tstr_put_lit(zse, "board") &&
	     uc_mgmt_put_tstr(zse, CONFIG_BOARD_TARGET, UC_MGMT_TSTR_MAX) &&
	     zcbor_tstr_put_lit(zse, "api") && zcbor_uint32_put(zse, UC_API_VERSION) &&

	     zcbor_tstr_put_lit(zse, "device") && zcbor_map_start_encode(zse, 2) &&
	     zcbor_tstr_put_lit(zse, "instance") && zcbor_uint32_put(zse, st.device_instance) &&
	     zcbor_tstr_put_lit(zse, "name") &&
	     uc_mgmt_put_tstr(zse, st.device_name, sizeof(st.device_name) - 1) &&
	     zcbor_map_end_encode(zse, 2) &&

	     zcbor_tstr_put_lit(zse, "net") && zcbor_map_start_encode(zse, 2) &&
	     zcbor_tstr_put_lit(zse, "ipv4") &&
	     uc_mgmt_put_tstr(zse, st.ipv4, sizeof(st.ipv4) - 1) &&
	     zcbor_tstr_put_lit(zse, "bacnet_port") && zcbor_uint32_put(zse, st.udp_port) &&
	     zcbor_map_end_encode(zse, 2) &&

	     zcbor_tstr_put_lit(zse, "uptime_s") &&
	     zcbor_uint64_put(zse, (uint64_t)(k_uptime_get() / MSEC_PER_SEC)) &&

	     zcbor_tstr_put_lit(zse, "fs") && zcbor_map_start_encode(zse, 3) &&
	     zcbor_tstr_put_lit(zse, "ready") && zcbor_bool_put(zse, fs_ready) &&
	     zcbor_tstr_put_lit(zse, "total") && zcbor_uint64_put(zse, fs_total) &&
	     zcbor_tstr_put_lit(zse, "free") && zcbor_uint64_put(zse, fs_free) &&
	     zcbor_map_end_encode(zse, 3) &&

	     zcbor_tstr_put_lit(zse, "bacnet") && zcbor_map_start_encode(zse, 2) &&
	     zcbor_tstr_put_lit(zse, "packets") && zcbor_uint32_put(zse, st.packets) &&
	     zcbor_tstr_put_lit(zse, "objects") && zcbor_uint32_put(zse, st.objects) &&
	     zcbor_map_end_encode(zse, 2) &&

	     zcbor_tstr_put_lit(zse, "apps") && zcbor_map_start_encode(zse, 2) &&
	     zcbor_tstr_put_lit(zse, "installed") && zcbor_uint32_put(zse, installed) &&
	     zcbor_tstr_put_lit(zse, "running") && zcbor_uint32_put(zse, running) &&
	     zcbor_map_end_encode(zse, 2) &&

	     zcbor_tstr_put_lit(zse, "wasm") && zcbor_map_start_encode(zse, 5) &&
	     zcbor_tstr_put_lit(zse, "interp") && zcbor_bool_put(zse, wi.interp) &&
	     zcbor_tstr_put_lit(zse, "aot") && zcbor_bool_put(zse, wi.aot) &&
	     zcbor_tstr_put_lit(zse, "aot_target") &&
	     uc_mgmt_put_tstr(zse, wi.aot_target, 32) &&
	     zcbor_tstr_put_lit(zse, "pool_total") && zcbor_uint32_put(zse, wi.pool_total) &&
	     zcbor_tstr_put_lit(zse, "pool_free") && zcbor_uint32_put(zse, wi.pool_free) &&
	     zcbor_map_end_encode(zse, 5);

	return MGMT_RETURN_CHECK(ok);
}

/* ---------------------------------------------------------------------- */
/* reload                                                                  */
/* ---------------------------------------------------------------------- */

static int doc_from_str(const char *s, uint32_t *docs)
{
	static const struct {
		const char *name;
		uint32_t docs;
	} map[] = {
		{ "device", UC_CFG_DEVICE },
		{ "io", UC_CFG_IO },
		{ "apps", UC_CFG_APPS },
		{ "all", UC_CFG_ALL },
	};

	for (size_t i = 0; i < ARRAY_SIZE(map); i++) {
		if (strcmp(s, map[i].name) == 0) {
			*docs = map[i].docs;
			return 0;
		}
	}

	return -EINVAL;
}

/* Apply one freshly loaded document to the running node. */
static int doc_apply(uint32_t doc, bool *reboot_required)
{
	int rc;

	switch (doc) {
	case UC_CFG_DEVICE:
		return uc_bn_apply_device_cfg(reboot_required);
	case UC_CFG_IO:
		rc = uc_io_apply_config();
		if (rc >= 0) {
			LOG_DBG("reload io: %d point(s) bound", rc);
		}
		return rc;
	case UC_CFG_APPS:
#if defined(CONFIG_UC_APPS)
		return uc_apps_reload();
#else
		/* apps.json is parsed and cached, nothing runs it */
		return 0;
#endif
	default:
		return -EINVAL;
	}
}

static const char *doc_name(uint32_t doc)
{
	switch (doc) {
	case UC_CFG_DEVICE:
		return "device";
	case UC_CFG_IO:
		return "io";
	case UC_CFG_APPS:
		return "apps";
	default:
		return "?";
	}
}

/* reload: {"doc": "device"|"io"|"apps"|"all"} -> {"reboot_required": bool}
 * Each document is loaded and applied on its own: a rejected document
 * keeps its active configuration and does not stop the others. The first
 * error is reported in "err" next to "reboot_required".
 */
static int node_reload(struct smp_streamer *ctxt)
{
	zcbor_state_t *zse = ctxt->writer->zs;
	char doc[DOC_TEXT_MAX];
	struct uc_mgmt_tstr doc_arg = { .buf = doc, .size = sizeof(doc) };
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("doc", uc_mgmt_dec_tstr, &doc_arg),
	};
	static const uint32_t order[] = { UC_CFG_DEVICE, UC_CFG_IO, UC_CFG_APPS };
	bool reboot_required = false;
	uint32_t docs = 0;
	int first_err = 0;
	bool ok;
	int rc;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	if ((rc == 0) && !uc_mgmt_found(req, ARRAY_SIZE(req), "doc")) {
		rc = -EINVAL;
	}
	if (rc == 0) {
		rc = doc_from_str(doc, &docs);
	}
	if (rc < 0) {
		return node_errno(ctxt, rc);
	}

	for (size_t i = 0; i < ARRAY_SIZE(order); i++) {
		bool reboot = false;

		if ((docs & order[i]) == 0U) {
			continue;
		}
		rc = uc_config_reload(order[i]);
		if (rc == 0) {
			rc = doc_apply(order[i], &reboot);
		}
		if (rc < 0) {
			LOG_WRN("reload %s: %d (%s)", doc_name(order[i]), rc, uc_err_str(rc));
			if (first_err == 0) {
				first_err = rc;
			}
			continue;
		}
		reboot_required = reboot_required || reboot;
		LOG_INF("reload %s: ok%s", doc_name(order[i]),
			reboot ? ", reboot required" : "");
	}

	ok = zcbor_tstr_put_lit(zse, "reboot_required") && zcbor_bool_put(zse, reboot_required);
	if (!ok) {
		return MGMT_ERR_EMSGSIZE;
	}

	return node_errno(ctxt, first_err);
}

/* ---------------------------------------------------------------------- */
/* objects                                                                 */
/* ---------------------------------------------------------------------- */

static void owner_str(uint8_t owner, char *buf, size_t size)
{
	if (owner == UC_OWNER_IO) {
		(void)uc_strlcpy(buf, "io", size);
	} else if (UC_OWNER_IS_APP(owner)) {
		char name[UC_APP_NAME_MAX] = "";

#if defined(CONFIG_UC_APPS)
		/* the owner slot is not the uc_apps_status() index */
		(void)uc_apps_owner_name(owner, name, sizeof(name));
#endif
		if (name[0] != '\0') {
			(void)snprintk(buf, size, "app:%s", name);
		} else {
			/* app gone or runtime not built in */
			(void)snprintk(buf, size, "app:#%u",
				       (unsigned int)UC_OWNER_APP_SLOT(owner));
		}
	} else {
		/* UC_OWNER_SYSTEM and objects of unknown origin */
		(void)uc_strlcpy(buf, "system", size);
	}
}

static bool put_object(zcbor_state_t *zse, const struct uc_bn_obj_info *o, const char *owner)
{
	return zcbor_map_start_encode(zse, 5) && zcbor_tstr_put_lit(zse, "type") &&
	       uc_mgmt_put_obj_type(zse, o->type) && zcbor_tstr_put_lit(zse, "instance") &&
	       zcbor_uint32_put(zse, o->instance) && zcbor_tstr_put_lit(zse, "name") &&
	       uc_mgmt_put_tstr(zse, o->name, sizeof(o->name) - 1) &&
	       zcbor_tstr_put_lit(zse, "owner") && uc_mgmt_put_tstr(zse, owner, OWNER_TEXT_MAX) &&
	       (!o->has_pv ||
		(zcbor_tstr_put_lit(zse, "pv") &&
		 uc_mgmt_put_value(zse, o->type, o->instance, PROP_PRESENT_VALUE, &o->pv))) &&
	       zcbor_map_end_encode(zse, 5);
}

/* objects: {"offset"?: uint, "count"?: uint} -> {"total": uint, "objects": [...]}
 * "objects" holds up to count entries (default OBJECTS_COUNT_DEFAULT) and
 * fewer when the response is full; the next page starts at
 * offset + len(objects).
 */
static int node_objects(struct smp_streamer *ctxt)
{
	zcbor_state_t *zse = ctxt->writer->zs;
	struct uc_bn_obj_info *chunk = uc_mgmt_scratch.objs;
	uint32_t offset = 0;
	uint32_t count = OBJECTS_COUNT_DEFAULT;
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("offset", zcbor_uint32_decode, &offset),
		ZCBOR_MAP_DECODE_KEY_DECODER("count", zcbor_uint32_decode, &count),
	};
	char owner[OWNER_TEXT_MAX];
	size_t total = 0;
	uint32_t n = 0;
	bool full = false;
	bool ok;
	int got;
	int rc;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	if (rc < 0) {
		return node_errno(ctxt, rc);
	}

	/* first chunk before encoding anything: gives "total" and errors */
	got = uc_bn_obj_list(offset, chunk, MIN(count, (uint32_t)UC_MGMT_OBJ_CHUNK), &total);
	if (got < 0) {
		return node_errno(ctxt, got);
	}

	ok = zcbor_tstr_put_lit(zse, "total") && zcbor_uint32_put(zse, (uint32_t)total) &&
	     zcbor_tstr_put_lit(zse, "objects") && zcbor_list_start_encode(zse, count);

	while (ok && !full && (got > 0)) {
		for (int i = 0; i < got; i++) {
			struct uc_mgmt_mark m;

			owner_str(chunk[i].owner, owner, sizeof(owner));
			uc_mgmt_mark_set(zse, &m);
			if (!uc_mgmt_elem_commit(ctxt, &m, put_object(zse, &chunk[i], owner))) {
				full = true;
				break;
			}
			n++;
		}
		if (full || (n >= count) || ((size_t)offset + n >= total)) {
			break;
		}
		got = uc_bn_obj_list((size_t)offset + n, chunk,
				     MIN(count - n, (uint32_t)UC_MGMT_OBJ_CHUNK), NULL);
	}

	ok = ok && zcbor_list_end_encode(zse, count);
	LOG_DBG("objects: offset %u, %u of %u", (unsigned int)offset, (unsigned int)n,
		(unsigned int)total);

	return MGMT_RETURN_CHECK(ok);
}

/* ---------------------------------------------------------------------- */
/* prop_read / prop_write                                                  */
/* ---------------------------------------------------------------------- */

/* prop_read: {"type": tstr|uint, "instance": uint, "prop": tstr|uint,
 *             "index"?: int} -> {"value": <value>}
 */
static int node_prop_read(struct smp_streamer *ctxt)
{
	zcbor_state_t *zse = ctxt->writer->zs;
	BACNET_APPLICATION_DATA_VALUE *value = &uc_mgmt_scratch.value;
	uint16_t type = 0;
	uint32_t instance = 0;
	uint32_t prop = 0;
	int32_t index = -1;
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("type", uc_mgmt_dec_obj_type, &type),
		ZCBOR_MAP_DECODE_KEY_DECODER("instance", zcbor_uint32_decode, &instance),
		ZCBOR_MAP_DECODE_KEY_DECODER("prop", uc_mgmt_dec_prop, &prop),
		ZCBOR_MAP_DECODE_KEY_DECODER("index", zcbor_int32_decode, &index),
	};
	int rc;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	if ((rc == 0) && (!uc_mgmt_found(req, ARRAY_SIZE(req), "type") ||
			  !uc_mgmt_found(req, ARRAY_SIZE(req), "instance") ||
			  !uc_mgmt_found(req, ARRAY_SIZE(req), "prop") ||
			  (instance > BACNET_MAX_INSTANCE))) {
		rc = -EINVAL;
	}
	if (rc == 0) {
		rc = uc_bn_prop_read(type, instance, prop, index, value);
	}
	if (rc < 0) {
		LOG_DBG("prop_read %u:%u prop %u: %d", type, instance, prop, rc);
		return node_errno(ctxt, rc);
	}

	return MGMT_RETURN_CHECK(zcbor_tstr_put_lit(zse, "value") &&
				 uc_mgmt_put_value(zse, type, instance, prop, value));
}

/* prop_write: {"type": tstr|uint, "instance": uint, "prop": tstr|uint,
 *              "value": <value>, "priority"?: uint, "index"?: int} -> {}
 */
static int node_prop_write(struct smp_streamer *ctxt)
{
	BACNET_APPLICATION_DATA_VALUE *bvalue = &uc_mgmt_scratch.value;
	struct uc_mgmt_value value;
	uint16_t type = 0;
	uint32_t instance = 0;
	uint32_t prop = 0;
	uint32_t priority = 0;
	int32_t index = -1;
	struct zcbor_map_decode_key_val req[] = {
		ZCBOR_MAP_DECODE_KEY_DECODER("type", uc_mgmt_dec_obj_type, &type),
		ZCBOR_MAP_DECODE_KEY_DECODER("instance", zcbor_uint32_decode, &instance),
		ZCBOR_MAP_DECODE_KEY_DECODER("prop", uc_mgmt_dec_prop, &prop),
		ZCBOR_MAP_DECODE_KEY_DECODER("value", uc_mgmt_dec_value, &value),
		ZCBOR_MAP_DECODE_KEY_DECODER("priority", zcbor_uint32_decode, &priority),
		ZCBOR_MAP_DECODE_KEY_DECODER("index", zcbor_int32_decode, &index),
	};
	int rc;

	rc = uc_mgmt_decode(ctxt, req, ARRAY_SIZE(req));
	if ((rc == 0) && (!uc_mgmt_found(req, ARRAY_SIZE(req), "type") ||
			  !uc_mgmt_found(req, ARRAY_SIZE(req), "instance") ||
			  !uc_mgmt_found(req, ARRAY_SIZE(req), "prop") ||
			  !uc_mgmt_found(req, ARRAY_SIZE(req), "value") ||
			  (instance > BACNET_MAX_INSTANCE) || (priority > BACNET_MAX_PRIORITY))) {
		rc = -EINVAL;
	}
	if (rc == 0) {
		rc = uc_mgmt_value_to_bacnet(&value, type, prop, bvalue);
	}
	if (rc == 0) {
		rc = uc_bn_prop_write(type, instance, prop, index, bvalue, (uint8_t)priority,
				      UC_OWNER_NONE);
		LOG_INF("prop_write %s:%u prop %u prio %u: %d", uc_obj_type_to_str(type), instance,
			prop, priority, rc);
	}

	return node_errno(ctxt, rc);
}

static const struct mgmt_handler uc_node_handlers[] = {
	[UC_MGMT_NODE_INFO] = { .mh_read = node_info },
	[UC_MGMT_NODE_RELOAD] = { .mh_write = node_reload },
	[UC_MGMT_NODE_OBJECTS] = { .mh_read = node_objects },
	[UC_MGMT_NODE_PROP_READ] = { .mh_read = node_prop_read },
	[UC_MGMT_NODE_PROP_WRITE] = { .mh_write = node_prop_write },
};

static struct mgmt_group uc_node_group = {
	.mg_handlers = uc_node_handlers,
	.mg_handlers_count = ARRAY_SIZE(uc_node_handlers),
	.mg_group_id = UC_MGMT_GROUP_NODE,
#if defined(CONFIG_MCUMGR_SMP_SUPPORT_ORIGINAL_PROTOCOL)
	.mg_translate_error = uc_mgmt_translate_error,
#endif
#if defined(CONFIG_MCUMGR_GRP_ENUM_DETAILS_NAME)
	.mg_group_name = "uc_node",
#endif
};

void uc_mgmt_node_register(void)
{
	mgmt_register_group(&uc_node_group);
}

/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * "uc" shell commands (console UART and MCUmgr shell group):
 *
 *   uc info
 *   uc app list | start <name> | stop <name> | remove <name> [-d]
 *   uc io list | read [name] | write <name> <value> | force <name> <value>
 *         | release <name>
 *   uc obj list [offset] [count]
 *   uc cfg show <device|io|apps> | reload <device|io|apps|all>
 *
 * Only the public module APIs are used. Commands may run concurrently (UART
 * shell thread and the MCUmgr shell backend), so large structures are
 * allocated per command from the kernel heap instead of static buffers or
 * the shell stack.
 */

#include <errno.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/shell/shell.h>

#include "bacnet/bacstr.h"

#include "uc/uc_common.h"
#include "uc/uc_storage.h"
#include "uc/uc_config.h"
#include "uc/uc_net.h"
#include "uc/uc_io.h"
#include "uc/uc_bacnet.h"
#if defined(CONFIG_UC_APPS)
#include "uc/uc_apps.h"
#endif

LOG_MODULE_REGISTER(uc_shell, CONFIG_UC_LOG_LEVEL);

#define OBJ_LIST_CHUNK 8

static int print_rc(const struct shell *sh, const char *what, int rc)
{
	if (rc < 0) {
		shell_error(sh, "%s: %d (%s)", what, rc, uc_err_str(rc));
		return rc;
	}
	return 0;
}

static int parse_double(const char *s, double *out)
{
	char *end;

	if (s == NULL || *s == '\0') {
		return -EINVAL;
	}
	*out = strtod(s, &end);
	return (*end == '\0') ? 0 : -EINVAL;
}

static int parse_size(const char *s, size_t *out)
{
	char *end;
	unsigned long v;

	if (s == NULL || *s == '\0' || *s == '-') {
		return -EINVAL;
	}
	v = strtoul(s, &end, 10);
	if (*end != '\0') {
		return -EINVAL;
	}
	*out = (size_t)v;
	return 0;
}

static void owner_str(uint8_t owner, char *buf, size_t len)
{
	switch (owner) {
	case UC_OWNER_NONE:
		(void)uc_strlcpy(buf, "-", len);
		break;
	case UC_OWNER_SYSTEM:
		(void)uc_strlcpy(buf, "system", len);
		break;
	case UC_OWNER_IO:
		(void)uc_strlcpy(buf, "io", len);
		break;
	default:
		if (UC_OWNER_IS_APP(owner)) {
			snprintk(buf, len, "app#%u", (unsigned int)UC_OWNER_APP_SLOT(owner));
		} else {
			snprintk(buf, len, "%u", owner);
		}
		break;
	}
}

static void value_str(const BACNET_APPLICATION_DATA_VALUE *v, char *buf, size_t len)
{
	double d;

	if (uc_value_to_double(v, &d) == 0) {
		snprintk(buf, len, "%g", d);
		return;
	}

	switch (v->tag) {
	case BACNET_APPLICATION_TAG_NULL:
		(void)uc_strlcpy(buf, "null", len);
		return;
#if defined(BACAPP_CHARACTER_STRING)
	case BACNET_APPLICATION_TAG_CHARACTER_STRING:
		/* characterstring_value() only reads but takes a non-const pointer */
		snprintk(buf, len, "\"%.*s\"",
			 (int)characterstring_length(&v->type.Character_String),
			 characterstring_value(
				 (BACNET_CHARACTER_STRING *)&v->type.Character_String));
		return;
#endif
	default:
		snprintk(buf, len, "<tag %u>", v->tag);
		return;
	}
}

/* ---------------------------------------------------------------------- */
/* uc info                                                                 */
/* ---------------------------------------------------------------------- */

static int cmd_info(const struct shell *sh, size_t argc, char **argv)
{
	struct uc_bn_status st;
	char ip[16];
	uint64_t total = 0;
	uint64_t free_bytes = 0;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	uc_bn_status_get(&st);
	uc_net_ipv4_str(ip, sizeof(ip));

	shell_print(sh, "fw:      %s", CONFIG_UC_FW_VERSION);
	shell_print(sh, "board:   %s (io catalog: %s)", CONFIG_BOARD_TARGET, uc_io_board_name());
	shell_print(sh, "uptime:  %u s", (unsigned int)(k_uptime_get() / 1000));
	shell_print(sh, "device:  %u \"%s\" (bacnet %s)", st.device_instance, st.device_name,
		    st.ready ? "ready" : "not ready");
	shell_print(sh, "net:     ipv4 %s, bacnet/ip udp %u", ip, st.udp_port);
	shell_print(sh, "bacnet:  %u packets, %u objects", st.packets, st.objects);

	if (uc_storage_ready() && uc_storage_stats(&total, &free_bytes) == 0) {
		shell_print(sh, "fs:      %s %u KiB total, %u KiB free", UC_FS_ROOT,
			    (unsigned int)(total / 1024U), (unsigned int)(free_bytes / 1024U));
	} else {
		shell_print(sh, "fs:      not ready");
	}

#if defined(CONFIG_UC_APPS)
	{
		struct uc_wasm_info wi;

		uc_apps_wasm_info(&wi);
		shell_print(sh, "apps:    %u installed, %u running",
			    (unsigned int)uc_apps_installed(), (unsigned int)uc_apps_running());
		shell_print(sh, "wasm:    interp %s, aot %s%s%s, pool %u/%u bytes free",
			    wi.interp ? "yes" : "no", wi.aot ? "yes" : "no",
			    (wi.aot && wi.aot_target != NULL) ? " " : "",
			    (wi.aot && wi.aot_target != NULL) ? wi.aot_target : "", wi.pool_free,
			    wi.pool_total);
	}
#else
	shell_print(sh, "apps:    not built in");
#endif

	return 0;
}

/* ---------------------------------------------------------------------- */
/* uc app                                                                  */
/* ---------------------------------------------------------------------- */

#if defined(CONFIG_UC_APPS)
static void print_perms(const struct shell *sh, uint32_t perms)
{
	char buf[64] = "";

	for (uint32_t bit = BIT(0); bit != 0U && bit <= UC_PERM_KV; bit <<= 1) {
		const char *name = uc_perm_to_str(bit);

		if ((perms & bit) == 0U || name == NULL) {
			continue;
		}
		if (buf[0] != '\0') {
			strncat(buf, ",", sizeof(buf) - strlen(buf) - 1);
		}
		strncat(buf, name, sizeof(buf) - strlen(buf) - 1);
	}
	shell_print(sh, "    perms: %s", (buf[0] != '\0') ? buf : "-");
}

static int cmd_app_list(const struct shell *sh, size_t argc, char **argv)
{
	struct uc_app_status *st;
	size_t n = uc_apps_installed();

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	if (n == 0) {
		shell_print(sh, "no applications installed");
		return 0;
	}

	st = k_malloc(sizeof(*st));
	if (st == NULL) {
		return print_rc(sh, "app list", -ENOMEM);
	}

	for (size_t i = 0; i < n; i++) {
		if (uc_apps_status(i, st) < 0) {
			continue;
		}
		shell_print(sh, "%-23s %-8s %s", st->cfg.name, uc_app_state_str(st->state),
			    st->cfg.file);
		shell_print(sh, "    autostart %s, period %u ms, heap %u KiB, stack %u KiB",
			    st->cfg.autostart ? "yes" : "no", st->cfg.period_ms, st->cfg.heap_kb,
			    st->cfg.stack_kb);
		print_perms(sh, st->cfg.perms);
		shell_print(sh, "    ticks %u, events %u, errors %u, uptime %u ms", st->ticks,
			    st->events, st->errors, (unsigned int)st->uptime_ms);
		if (st->last_error[0] != '\0') {
			shell_print(sh, "    last error: %s", st->last_error);
		}
	}

	k_free(st);
	return 0;
}

static int cmd_app_start(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	return print_rc(sh, "app start", uc_apps_start(argv[1]));
}

static int cmd_app_stop(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	return print_rc(sh, "app stop", uc_apps_stop(argv[1]));
}

static int cmd_app_remove(const struct shell *sh, size_t argc, char **argv)
{
	bool delete_file = false;

	if (argc > 2) {
		if (strcmp(argv[2], "-d") != 0) {
			shell_error(sh, "usage: uc app remove <name> [-d]");
			return -EINVAL;
		}
		delete_file = true;
	}
	return print_rc(sh, "app remove", uc_apps_remove(argv[1], delete_file));
}

SHELL_STATIC_SUBCMD_SET_CREATE(
	sub_uc_app,
	SHELL_CMD_ARG(list, NULL, "List installed applications", cmd_app_list, 1, 0),
	SHELL_CMD_ARG(start, NULL, "Start an application: start <name>", cmd_app_start, 2, 0),
	SHELL_CMD_ARG(stop, NULL, "Stop an application: stop <name>", cmd_app_stop, 2, 0),
	SHELL_CMD_ARG(remove, NULL,
		      "Remove an application: remove <name> [-d] (-d: delete module and data)",
		      cmd_app_remove, 2, 1),
	SHELL_SUBCMD_SET_END);
#endif /* CONFIG_UC_APPS */

/* ---------------------------------------------------------------------- */
/* uc io                                                                   */
/* ---------------------------------------------------------------------- */

static int find_channel(const struct shell *sh, const char *name)
{
	int id = uc_io_find(name);

	if (id < 0) {
		shell_error(sh, "unknown channel '%s'", name);
	}
	return id;
}

static int cmd_io_list(const struct shell *sh, size_t argc, char **argv)
{
	struct uc_io_channel_info info;
	size_t n = uc_io_channel_count();

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "board %s, %u channels", uc_io_board_name(), (unsigned int)n);
	for (size_t i = 0; i < n; i++) {
		if (uc_io_channel_info((int)i, &info) < 0) {
			continue;
		}
		if (info.bound) {
			shell_print(sh, "%2d %-15s %s %-4s %s -> %s:%u  %s", info.id, info.name,
				    uc_io_kind_str(info.kind), uc_io_hw_str(info.hw),
				    info.forced ? "forced" : "      ",
				    uc_obj_type_to_str(info.obj_type), info.obj_instance,
				    (info.desc != NULL) ? info.desc : "");
		} else {
			shell_print(sh, "%2d %-15s %s %-4s %s  %s", info.id, info.name,
				    uc_io_kind_str(info.kind), uc_io_hw_str(info.hw),
				    info.forced ? "forced" : "      ",
				    (info.desc != NULL) ? info.desc : "");
		}
	}
	return 0;
}

static int cmd_io_read(const struct shell *sh, size_t argc, char **argv)
{
	struct uc_io_channel_info info;
	double v;
	int rc;

	if (argc > 1) {
		int id = find_channel(sh, argv[1]);

		if (id < 0) {
			return id;
		}
		rc = uc_io_read(id, &v);
		if (rc < 0) {
			return print_rc(sh, "io read", rc);
		}
		shell_print(sh, "%s = %g", argv[1], v);
		return 0;
	}

	for (size_t i = 0; i < uc_io_channel_count(); i++) {
		if (uc_io_channel_info((int)i, &info) < 0) {
			continue;
		}
		rc = uc_io_read((int)i, &v);
		if (rc < 0) {
			shell_print(sh, "%-15s error %d", info.name, rc);
		} else {
			shell_print(sh, "%-15s %g%s", info.name, v, info.forced ? " (forced)" : "");
		}
	}
	return 0;
}

static int io_value_cmd(const struct shell *sh, char **argv, const char *what,
			int (*fn)(int id, double value))
{
	double v;
	int id = find_channel(sh, argv[1]);

	if (id < 0) {
		return id;
	}
	if (parse_double(argv[2], &v) < 0) {
		shell_error(sh, "invalid value '%s'", argv[2]);
		return -EINVAL;
	}
	return print_rc(sh, what, fn(id, v));
}

static int cmd_io_write(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	return io_value_cmd(sh, argv, "io write", uc_io_write);
}

static int cmd_io_force(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	return io_value_cmd(sh, argv, "io force", uc_io_force);
}

static int cmd_io_release(const struct shell *sh, size_t argc, char **argv)
{
	int id;

	ARG_UNUSED(argc);

	id = find_channel(sh, argv[1]);
	if (id < 0) {
		return id;
	}
	return print_rc(sh, "io release", uc_io_release(id));
}

SHELL_STATIC_SUBCMD_SET_CREATE(
	sub_uc_io,
	SHELL_CMD_ARG(list, NULL, "List IO channels", cmd_io_list, 1, 0),
	SHELL_CMD_ARG(read, NULL, "Read channels: read [name]", cmd_io_read, 1, 1),
	SHELL_CMD_ARG(write, NULL, "Drive an output: write <name> <value>", cmd_io_write, 3, 0),
	SHELL_CMD_ARG(force, NULL, "Override a channel: force <name> <value>", cmd_io_force, 3,
		      0),
	SHELL_CMD_ARG(release, NULL, "Release an override: release <name>", cmd_io_release, 2,
		      0),
	SHELL_SUBCMD_SET_END);

/* ---------------------------------------------------------------------- */
/* uc obj                                                                  */
/* ---------------------------------------------------------------------- */

static int cmd_obj_list(const struct shell *sh, size_t argc, char **argv)
{
	struct uc_bn_obj_info *objs;
	size_t offset = 0;
	size_t count = SIZE_MAX;
	size_t total = 0;
	size_t shown = 0;
	char owner[16];
	char value[40];

	if (argc > 1 && parse_size(argv[1], &offset) < 0) {
		shell_error(sh, "invalid offset '%s'", argv[1]);
		return -EINVAL;
	}
	if (argc > 2 && parse_size(argv[2], &count) < 0) {
		shell_error(sh, "invalid count '%s'", argv[2]);
		return -EINVAL;
	}

	objs = k_malloc(sizeof(*objs) * OBJ_LIST_CHUNK);
	if (objs == NULL) {
		return print_rc(sh, "obj list", -ENOMEM);
	}

	while (shown < count) {
		size_t want = MIN((size_t)OBJ_LIST_CHUNK, count - shown);
		int n = uc_bn_obj_list(offset + shown, objs, want, &total);

		if (n < 0) {
			k_free(objs);
			return print_rc(sh, "obj list", n);
		}
		for (int i = 0; i < n; i++) {
			owner_str(objs[i].owner, owner, sizeof(owner));
			if (objs[i].has_pv) {
				value_str(&objs[i].pv, value, sizeof(value));
			} else {
				(void)uc_strlcpy(value, "", sizeof(value));
			}
			shell_print(sh, "%-20s %7u %-8s %-24s %s",
				    uc_obj_type_to_str(objs[i].type), objs[i].instance, owner,
				    objs[i].name, value);
		}
		shown += (size_t)n;
		if (n == 0 || (size_t)n < want) {
			break;
		}
	}

	shell_print(sh, "%u of %u objects", (unsigned int)shown, (unsigned int)total);
	k_free(objs);
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(sub_uc_obj,
			       SHELL_CMD_ARG(list, NULL,
					     "List local BACnet objects: list [offset] [count]",
					     cmd_obj_list, 1, 2),
			       SHELL_SUBCMD_SET_END);

/* ---------------------------------------------------------------------- */
/* uc cfg                                                                  */
/* ---------------------------------------------------------------------- */

static int doc_from_str(const char *s, uint32_t *doc)
{
	if (strcmp(s, "device") == 0) {
		*doc = UC_CFG_DEVICE;
	} else if (strcmp(s, "io") == 0) {
		*doc = UC_CFG_IO;
	} else if (strcmp(s, "apps") == 0) {
		*doc = UC_CFG_APPS;
	} else if (strcmp(s, "all") == 0) {
		*doc = UC_CFG_ALL;
	} else {
		return -EINVAL;
	}
	return 0;
}

static const char *log_level_str(uint8_t level)
{
	static const char *const names[] = {"none", "err", "wrn", "inf", "dbg"};

	return (level < ARRAY_SIZE(names)) ? names[level] : "?";
}

static void show_device(const struct shell *sh)
{
	struct uc_device_cfg *c = k_malloc(sizeof(*c));

	if (c == NULL) {
		(void)print_rc(sh, "cfg show", -ENOMEM);
		return;
	}
	uc_config_get_device(c);

	shell_print(sh, "device:  instance %u, name \"%s\"", c->instance, c->name);
	shell_print(sh, "         description \"%s\", location \"%s\"", c->description,
		    c->location);
	if (c->dhcp) {
		shell_print(sh, "network: dhcp");
	} else {
		shell_print(sh, "network: static %s/%s gw %s", c->ipv4, c->netmask,
			    (c->gateway[0] != '\0') ? c->gateway : "-");
	}
	shell_print(sh, "bacnet:  udp %u, apdu timeout %u ms, retries %u", c->udp_port,
		    c->apdu_timeout_ms, c->apdu_retries);
	if (c->fd_enabled) {
		shell_print(sh, "         foreign device: bbmd %s:%u ttl %u s", c->fd_bbmd,
			    c->fd_port, c->fd_ttl_s);
	}
	for (size_t i = 0; i < c->binding_count; i++) {
		shell_print(sh, "         binding: device %u -> %s:%u", c->bindings[i].device,
			    c->bindings[i].address, c->bindings[i].port);
	}
	shell_print(sh, "log:     %s", log_level_str(c->log_level));
	k_free(c);
}

static void show_io(const struct shell *sh)
{
	struct uc_io_cfg *c = k_malloc(sizeof(*c));

	if (c == NULL) {
		(void)print_rc(sh, "cfg show", -ENOMEM);
		return;
	}
	uc_config_get_io(c);

	shell_print(sh, "%u points", (unsigned int)c->count);
	for (size_t i = 0; i < c->count; i++) {
		const struct uc_io_point_cfg *p = &c->points[i];

		shell_print(sh, "%-15s -> %s:%u \"%s\"", p->channel, uc_obj_type_to_str(p->object_type),
			    p->object_instance, p->name);
		shell_print(sh, "    units %u, scale %g, offset %g, cov %g, sample %u ms, "
			    "debounce %u ms%s", p->units, p->scale, p->offset, p->cov_increment,
			    p->sample_ms, p->debounce_ms, p->invert ? ", inverted" : "");
		if (p->has_min) {
			shell_print(sh, "    min %g", p->min);
		}
		if (p->has_max) {
			shell_print(sh, "    max %g", p->max);
		}
	}
	k_free(c);
}

static void show_apps(const struct shell *sh)
{
	struct uc_apps_cfg *c = k_malloc(sizeof(*c));

	if (c == NULL) {
		(void)print_rc(sh, "cfg show", -ENOMEM);
		return;
	}
	uc_config_get_apps(c);

	shell_print(sh, "%u apps", (unsigned int)c->count);
	for (size_t i = 0; i < c->count; i++) {
		const struct uc_app_cfg *a = &c->apps[i];

		shell_print(sh, "%s: %s, autostart %s, period %u ms, heap %u KiB, stack %u KiB",
			    a->name, a->file, a->autostart ? "yes" : "no", a->period_ms,
			    a->heap_kb, a->stack_kb);
		for (uint32_t bit = BIT(0); bit <= UC_PERM_KV; bit <<= 1) {
			if ((a->perms & bit) != 0U && uc_perm_to_str(bit) != NULL) {
				shell_print(sh, "    perm %s", uc_perm_to_str(bit));
			}
		}
		for (size_t k = 0; k < a->param_count; k++) {
			shell_print(sh, "    param %s = \"%s\"", a->params[k].key,
				    a->params[k].value);
		}
		if (a->has_sha256) {
			char hex[2 * sizeof(a->sha256) + 1];

			for (size_t k = 0; k < sizeof(a->sha256); k++) {
				snprintk(&hex[2 * k], 3, "%02x", a->sha256[k]);
			}
			shell_print(sh, "    sha256 %s", hex);
		}
	}
	k_free(c);
}

static int cmd_cfg_show(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t doc;

	ARG_UNUSED(argc);

	if (doc_from_str(argv[1], &doc) < 0 || doc == UC_CFG_ALL) {
		shell_error(sh, "usage: uc cfg show <device|io|apps>");
		return -EINVAL;
	}

	switch (doc) {
	case UC_CFG_DEVICE:
		show_device(sh);
		break;
	case UC_CFG_IO:
		show_io(sh);
		break;
	default:
		show_apps(sh);
		break;
	}
	return 0;
}

static int cmd_cfg_reload(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t doc;
	int rc;

	ARG_UNUSED(argc);

	if (doc_from_str(argv[1], &doc) < 0) {
		shell_error(sh, "usage: uc cfg reload <device|io|apps|all>");
		return -EINVAL;
	}

	rc = uc_config_reload(doc);
	if (rc < 0) {
		return print_rc(sh, "cfg reload", rc);
	}

	if ((doc & UC_CFG_DEVICE) != 0U) {
		bool reboot = false;

		rc = uc_bn_apply_device_cfg(&reboot);
		if (rc < 0) {
			return print_rc(sh, "apply device.json", rc);
		}
		if (reboot) {
			shell_warn(sh, "device.json: reboot required for instance/network/port");
		}
	}
	if ((doc & UC_CFG_IO) != 0U) {
		rc = uc_io_apply_config();
		if (rc < 0) {
			return print_rc(sh, "apply io.json", rc);
		}
		shell_print(sh, "io.json: %d points bound", rc);
	}
#if defined(CONFIG_UC_APPS)
	if ((doc & UC_CFG_APPS) != 0U) {
		rc = uc_apps_reload();
		if (rc < 0) {
			return print_rc(sh, "apply apps.json", rc);
		}
	}
#endif

	shell_print(sh, "reloaded");
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(
	sub_uc_cfg,
	SHELL_CMD_ARG(show, NULL, "Show the active configuration: show <device|io|apps>",
		      cmd_cfg_show, 2, 0),
	SHELL_CMD_ARG(reload, NULL,
		      "Re-read and apply a document: reload <device|io|apps|all>",
		      cmd_cfg_reload, 2, 0),
	SHELL_SUBCMD_SET_END);

/* ---------------------------------------------------------------------- */

SHELL_STATIC_SUBCMD_SET_CREATE(
	sub_uc,
	SHELL_CMD_ARG(info, NULL, "Node information", cmd_info, 1, 0),
#if defined(CONFIG_UC_APPS)
	SHELL_CMD(app, &sub_uc_app, "WebAssembly applications", NULL),
#endif
	SHELL_CMD(io, &sub_uc_io, "IO channels", NULL),
	SHELL_CMD(obj, &sub_uc_obj, "Local BACnet objects", NULL),
	SHELL_CMD(cfg, &sub_uc_cfg, "Configuration documents", NULL),
	SHELL_SUBCMD_SET_END);

SHELL_CMD_REGISTER(uc, &sub_uc, "BACnet-uc commands", NULL);

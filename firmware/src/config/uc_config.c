/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Configuration documents /lfs/cfg/{device,io,apps}.json (see schemas/):
 * parsers, apps.json encoder and the thread-safe RAM cache.
 *
 * Parsing strategy with Zephyr's JSON library (lib/utils/json.c):
 *  - json_obj_parse() returns the bitmap of decoded fields for the top-level
 *    object only; for nested objects and array elements that information is
 *    lost. Numbers are therefore decoded as JSON_TOK_FLOAT tokens (pointer
 *    and length of the number text, pointer NULL when the key is absent) and
 *    converted and range checked here with strtoll()/strtod(). Strings use
 *    JSON_TOK_STRING_BUF (unescaped into a fixed buffer; too long -> error;
 *    empty when absent). Booleans are preset to their default before
 *    parsing. The input buffer is not modified by these token types.
 *  - Keys unknown to the descriptors are skipped by the library. The schemas
 *    forbid them ("additionalProperties": false); the firmware tolerates them.
 *  - Array overflow: the library returns -ENOSPC for a too long array in an
 *    object; an overflow inside an array element (apps[].params, apps[].perms)
 *    is reported by the library as a syntax error (-EINVAL).
 *
 * The parse scratch structures and the cache are large (io.json: ~11 KB
 * scratch + ~7 KB result); they live on the kernel heap or in .bss, never on
 * the caller's stack.
 *
 * Staged documents: a client uploads <doc>.new (SMP fs group) and requests a
 * reload; load_doc() validates the staged file and renames it over the
 * active document only when it is valid, so a half-written or invalid
 * upload never replaces a working configuration.
 */

#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/data/json.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/sys/printk.h>

#include "bacnet/bacenum.h"

#include "uc/uc_config.h"
#include "uc/uc_storage.h"

LOG_MODULE_REGISTER(uc_config, CONFIG_UC_LOG_LEVEL);

#define UC_BACNET_PORT_DEFAULT 47808 /* 0xBAC0 */
#define UC_PASSWORD_MAX        20    /* BACnet: CharacterString (SIZE(1..20)) */
#define UC_INSTANCE_MAX        4194302
#define UC_SCHEMA_VERSION      1

#define UC_PARAM_KEY_MAX   (sizeof(((struct uc_app_param *)0)->key) - 1)
#define UC_APP_FILE_PREFIX UC_DIR_APPS "/"
#define UC_APP_FILE_STEM_MAX 40

/* ---------------------------------------------------------------------- */
/* Defaults                                                                */
/* ---------------------------------------------------------------------- */

void uc_config_device_defaults(struct uc_device_cfg *cfg)
{
	memset(cfg, 0, sizeof(*cfg));
	cfg->instance = CONFIG_UC_DEVICE_INSTANCE_DEFAULT;
	(void)uc_strlcpy(cfg->name, CONFIG_UC_DEVICE_NAME_DEFAULT, sizeof(cfg->name));
	cfg->dhcp = true;
	cfg->udp_port = UC_BACNET_PORT_DEFAULT;
	cfg->apdu_timeout_ms = 3000;
	cfg->apdu_retries = 3;
	cfg->fd_enabled = false;
	cfg->fd_port = UC_BACNET_PORT_DEFAULT;
	cfg->fd_ttl_s = 60;
	cfg->binding_count = 0;
	cfg->log_level = LOG_LEVEL_INF;
}

void uc_config_app_defaults(struct uc_app_cfg *cfg)
{
	memset(cfg, 0, sizeof(*cfg));
	cfg->autostart = true;
	cfg->period_ms = 1000;
	cfg->heap_kb = 8;
	cfg->stack_kb = 4;
}

void uc_config_io_point_defaults(struct uc_io_point_cfg *pt)
{
	memset(pt, 0, sizeof(*pt));
	pt->units = UNITS_NO_UNITS;
	pt->scale = 1.0;
	pt->offset = 0.0;
	pt->cov_increment = 0.1;
	pt->sample_ms = 100;
	pt->debounce_ms = 20;
	pt->invert = false;
}

/* ---------------------------------------------------------------------- */
/* Token helpers                                                           */
/* ---------------------------------------------------------------------- */

#define NUM_TEXT_MAX 40

static int num_text(const struct json_obj_token *t, char *buf)
{
	if (t->start == NULL) {
		return -ENOENT;
	}
	if (t->length == 0 || t->length >= NUM_TEXT_MAX) {
		return -EINVAL;
	}
	memcpy(buf, t->start, t->length);
	buf[t->length] = '\0';
	return 0;
}

/* Integer in [min, max]: 0, -ENOENT (absent) or -EINVAL. */
static int tok_int(const struct json_obj_token *t, int64_t min, int64_t max, int64_t *out)
{
	char buf[NUM_TEXT_MAX];
	char *end;
	long long v;
	int rc;

	rc = num_text(t, buf);
	if (rc < 0) {
		return rc;
	}

	errno = 0;
	v = strtoll(buf, &end, 10);
	if (errno != 0 || end == buf || *end != '\0' || v < min || v > max) {
		return -EINVAL;
	}

	*out = v;
	return 0;
}

/* Finite number: 0, -ENOENT (absent) or -EINVAL. */
static int tok_num(const struct json_obj_token *t, double *out)
{
	char buf[NUM_TEXT_MAX];
	char *end;
	double v;
	int rc;

	rc = num_text(t, buf);
	if (rc < 0) {
		return rc;
	}

	errno = 0;
	v = strtod(buf, &end);
	if (errno != 0 || end == buf || *end != '\0' || !isfinite(v)) {
		return -EINVAL;
	}

	*out = v;
	return 0;
}

static int bad(const char *doc, int idx, const char *field)
{
	if (idx >= 0) {
		LOG_WRN("%s: invalid or missing %s (entry %d)", doc, field, idx);
	} else {
		LOG_WRN("%s: invalid or missing %s", doc, field);
	}
	return -EINVAL;
}

/* Optional integer field: keeps the default in dst when absent. */
#define OPT_INT(tok, min, max, dst, doc, idx, field)                                              \
	do {                                                                                       \
		int64_t v_;                                                                        \
		int r_ = tok_int((tok), (min), (max), &v_);                                        \
		if (r_ == 0) {                                                                     \
			(dst) = (__typeof__(dst))v_;                                               \
		} else if (r_ != -ENOENT) {                                                        \
			return bad((doc), (idx), (field));                                         \
		}                                                                                  \
	} while (0)

/* Required integer field. */
#define REQ_INT(tok, min, max, dst, doc, idx, field)                                              \
	do {                                                                                       \
		int64_t v_;                                                                        \
		if (tok_int((tok), (min), (max), &v_) != 0) {                                      \
			return bad((doc), (idx), (field));                                         \
		}                                                                                  \
		(dst) = (__typeof__(dst))v_;                                                       \
	} while (0)

static bool schema_ok(uint64_t fields, uint64_t bit, const struct json_obj_token *t)
{
	int64_t v;

	return (fields & bit) != 0U &&
	       tok_int(t, UC_SCHEMA_VERSION, UC_SCHEMA_VERSION, &v) == 0;
}

static bool ipv4_parse(const char *s, uint32_t *out)
{
	uint32_t addr = 0;
	const char *p = s;

	for (int part = 0; part < 4; part++) {
		uint32_t v = 0;
		int digits = 0;

		while (*p >= '0' && *p <= '9') {
			v = v * 10U + (uint32_t)(*p - '0');
			digits++;
			p++;
			if (digits > 3) {
				return false;
			}
		}
		if (digits == 0 || v > 255U) {
			return false;
		}
		addr = (addr << 8) | v;
		if (part < 3) {
			if (*p != '.') {
				return false;
			}
			p++;
		}
	}

	if (*p != '\0') {
		return false;
	}
	if (out != NULL) {
		*out = addr;
	}
	return true;
}

static bool ipv4_valid(const char *s)
{
	return ipv4_parse(s, NULL);
}

static bool netmask_valid(const char *s)
{
	uint32_t m;

	if (!ipv4_parse(s, &m)) {
		return false;
	}
	/* contiguous ones followed by zeros */
	m = ~m;
	return (m & (m + 1U)) == 0U;
}

/* True if buf holds a NUL-terminated string within size bytes. */
static bool str_fits(const char *buf, size_t size)
{
	return memchr(buf, '\0', size) != NULL;
}

static bool is_hex_lower(char c)
{
	return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
}

static uint8_t hex_val(char c)
{
	return (c <= '9') ? (uint8_t)(c - '0') : (uint8_t)(c - 'a' + 10);
}

static bool sha256_parse(const char *s, uint8_t digest[32])
{
	if (strlen(s) != 64) {
		return false;
	}
	for (size_t i = 0; i < 64; i++) {
		if (!is_hex_lower(s[i])) {
			return false;
		}
	}
	for (size_t i = 0; i < 32; i++) {
		digest[i] = (uint8_t)((hex_val(s[2 * i]) << 4) | hex_val(s[2 * i + 1]));
	}
	return true;
}

static bool file_char_valid(char c)
{
	return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
	       c == '_' || c == '.' || c == '-';
}

/* ^/lfs/apps/[A-Za-z0-9_.-]{1,40}\.(wasm|aot)$ */
static bool app_file_valid(const char *path)
{
	static const char *const suffixes[] = {".wasm", ".aot"};
	size_t plen = strlen(UC_APP_FILE_PREFIX);
	size_t len = strlen(path);

	if (len <= plen || strncmp(path, UC_APP_FILE_PREFIX, plen) != 0) {
		return false;
	}

	for (size_t s = 0; s < ARRAY_SIZE(suffixes); s++) {
		size_t slen = strlen(suffixes[s]);
		size_t stem;

		if (len < plen + slen || strcmp(path + len - slen, suffixes[s]) != 0) {
			continue;
		}
		stem = len - plen - slen;
		if (stem < 1 || stem > UC_APP_FILE_STEM_MAX) {
			return false;
		}
		for (size_t i = plen; i < plen + stem; i++) {
			if (!file_char_valid(path[i])) {
				return false;
			}
		}
		return true;
	}

	return false;
}

static int map_parse_error(int64_t ret, const char *doc)
{
	if (ret == -ENOSPC) {
		LOG_WRN("%s: table limit exceeded", doc);
		return -ENOSPC;
	}
	LOG_WRN("%s: JSON syntax or type error (%d)", doc, (int)ret);
	return -EINVAL;
}

/* ---------------------------------------------------------------------- */
/* device.json                                                             */
/* ---------------------------------------------------------------------- */

struct jd_device {
	struct json_obj_token instance;
	char name[UC_NAME_MAX];
	char description[UC_NAME_MAX];
	char location[UC_NAME_MAX];
};

struct jd_network {
	bool dhcp;
	char ipv4[16];
	char netmask[16];
	char gateway[16];
};

struct jd_fd {
	char bbmd[16];
	struct json_obj_token port;
	struct json_obj_token ttl_s;
};

struct jd_binding {
	struct json_obj_token device;
	char address[16];
	struct json_obj_token port;
};

struct jd_bacnet {
	struct json_obj_token udp_port;
	struct json_obj_token apdu_timeout_ms;
	struct json_obj_token apdu_retries;
	struct jd_fd foreign_device;
	struct jd_binding static_bindings[CONFIG_UC_BACNET_STATIC_BINDINGS_MAX];
	size_t static_bindings_len;
	/* larger than the limit so that a long password gets its own message */
	char password[UC_NAME_MAX];
};

struct jd_log {
	char level[8];
};

struct jd_doc {
	struct json_obj_token schema;
	struct jd_device device;
	struct jd_network network;
	struct jd_bacnet bacnet;
	struct jd_log log;
};

static const struct json_obj_descr jd_device_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct jd_device, instance, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct jd_device, name, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct jd_device, description, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct jd_device, location, JSON_TOK_STRING_BUF),
};

static const struct json_obj_descr jd_network_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct jd_network, dhcp, JSON_TOK_TRUE),
	JSON_OBJ_DESCR_PRIM(struct jd_network, ipv4, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct jd_network, netmask, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct jd_network, gateway, JSON_TOK_STRING_BUF),
};

static const struct json_obj_descr jd_fd_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct jd_fd, bbmd, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct jd_fd, port, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct jd_fd, ttl_s, JSON_TOK_FLOAT),
};

static const struct json_obj_descr jd_binding_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct jd_binding, device, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct jd_binding, address, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct jd_binding, port, JSON_TOK_FLOAT),
};

static const struct json_obj_descr jd_bacnet_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct jd_bacnet, udp_port, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct jd_bacnet, apdu_timeout_ms, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct jd_bacnet, apdu_retries, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_OBJECT(struct jd_bacnet, foreign_device, jd_fd_descr),
	JSON_OBJ_DESCR_OBJ_ARRAY(struct jd_bacnet, static_bindings,
				 CONFIG_UC_BACNET_STATIC_BINDINGS_MAX, static_bindings_len,
				 jd_binding_descr, ARRAY_SIZE(jd_binding_descr)),
	JSON_OBJ_DESCR_PRIM(struct jd_bacnet, password, JSON_TOK_STRING_BUF),
};

static const struct json_obj_descr jd_log_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct jd_log, level, JSON_TOK_STRING_BUF),
};

/* Order defines the bits of the json_obj_parse() result. */
static const struct json_obj_descr jd_doc_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct jd_doc, schema, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_OBJECT(struct jd_doc, device, jd_device_descr),
	JSON_OBJ_DESCR_OBJECT(struct jd_doc, network, jd_network_descr),
	JSON_OBJ_DESCR_OBJECT(struct jd_doc, bacnet, jd_bacnet_descr),
	JSON_OBJ_DESCR_OBJECT(struct jd_doc, log, jd_log_descr),
};

#define JD_HAS_SCHEMA BIT(0)
#define JD_HAS_DEVICE BIT(1)

static const struct {
	const char *name;
	uint8_t level;
} log_levels[] = {
	{"err", LOG_LEVEL_ERR},
	{"wrn", LOG_LEVEL_WRN},
	{"inf", LOG_LEVEL_INF},
	{"dbg", LOG_LEVEL_DBG},
};

static int device_from_json(const struct jd_doc *d, uint64_t fields, struct uc_device_cfg *out)
{
	static const char doc[] = "device.json";

	if (!schema_ok(fields, JD_HAS_SCHEMA, &d->schema)) {
		return bad(doc, -1, "schema");
	}
	if ((fields & JD_HAS_DEVICE) == 0U) {
		return bad(doc, -1, "device");
	}

	/* device */
	REQ_INT(&d->device.instance, 0, UC_INSTANCE_MAX, out->instance, doc, -1,
		"device.instance");
	if (d->device.name[0] == '\0') {
		return bad(doc, -1, "device.name");
	}
	(void)uc_strlcpy(out->name, d->device.name, sizeof(out->name));
	(void)uc_strlcpy(out->description, d->device.description, sizeof(out->description));
	(void)uc_strlcpy(out->location, d->device.location, sizeof(out->location));

	/* network */
	out->dhcp = d->network.dhcp;
	if (d->network.ipv4[0] != '\0') {
		if (!ipv4_valid(d->network.ipv4)) {
			return bad(doc, -1, "network.ipv4");
		}
		(void)uc_strlcpy(out->ipv4, d->network.ipv4, sizeof(out->ipv4));
	}
	if (d->network.netmask[0] != '\0') {
		if (!netmask_valid(d->network.netmask)) {
			return bad(doc, -1, "network.netmask");
		}
		(void)uc_strlcpy(out->netmask, d->network.netmask, sizeof(out->netmask));
	}
	if (d->network.gateway[0] != '\0') {
		if (!ipv4_valid(d->network.gateway)) {
			return bad(doc, -1, "network.gateway");
		}
		(void)uc_strlcpy(out->gateway, d->network.gateway, sizeof(out->gateway));
	}
	if (!out->dhcp) {
		if (out->ipv4[0] == '\0') {
			LOG_WRN("%s: network.dhcp is false but no ipv4 given, using DHCP", doc);
			out->dhcp = true;
		} else if (out->netmask[0] == '\0') {
			(void)uc_strlcpy(out->netmask, "255.255.255.0", sizeof(out->netmask));
		}
	}

	/* bacnet */
	OPT_INT(&d->bacnet.udp_port, 1, UINT16_MAX, out->udp_port, doc, -1, "bacnet.udp_port");
	OPT_INT(&d->bacnet.apdu_timeout_ms, 100, 60000, out->apdu_timeout_ms, doc, -1,
		"bacnet.apdu_timeout_ms");
	OPT_INT(&d->bacnet.apdu_retries, 0, 10, out->apdu_retries, doc, -1,
		"bacnet.apdu_retries");

	if (d->bacnet.foreign_device.bbmd[0] != '\0') {
		if (!ipv4_valid(d->bacnet.foreign_device.bbmd)) {
			return bad(doc, -1, "bacnet.foreign_device.bbmd");
		}
		out->fd_enabled = true;
		(void)uc_strlcpy(out->fd_bbmd, d->bacnet.foreign_device.bbmd,
				 sizeof(out->fd_bbmd));
		OPT_INT(&d->bacnet.foreign_device.port, 1, UINT16_MAX, out->fd_port, doc, -1,
			"bacnet.foreign_device.port");
		OPT_INT(&d->bacnet.foreign_device.ttl_s, 10, UINT16_MAX, out->fd_ttl_s, doc, -1,
			"bacnet.foreign_device.ttl_s");
	} else if (d->bacnet.foreign_device.port.start != NULL ||
		   d->bacnet.foreign_device.ttl_s.start != NULL) {
		/* foreign_device object present without its required bbmd */
		return bad(doc, -1, "bacnet.foreign_device.bbmd");
	}

	out->binding_count = d->bacnet.static_bindings_len;
	for (size_t i = 0; i < d->bacnet.static_bindings_len; i++) {
		const struct jd_binding *b = &d->bacnet.static_bindings[i];
		struct uc_static_binding *o = &out->bindings[i];

		REQ_INT(&b->device, 0, UC_INSTANCE_MAX, o->device, doc, (int)i,
			"bacnet.static_bindings.device");
		if (!ipv4_valid(b->address)) {
			return bad(doc, (int)i, "bacnet.static_bindings.address");
		}
		(void)uc_strlcpy(o->address, b->address, sizeof(o->address));
		o->port = UC_BACNET_PORT_DEFAULT;
		OPT_INT(&b->port, 1, UINT16_MAX, o->port, doc, (int)i,
			"bacnet.static_bindings.port");
	}

	/* The schema requires 1..20 printable ASCII characters; "" (absent)
	 * means no password.
	 */
	if (d->bacnet.password[0] != '\0') {
		if (strlen(d->bacnet.password) > UC_PASSWORD_MAX) {
			return bad(doc, -1, "bacnet.password");
		}
		for (const char *c = d->bacnet.password; *c != '\0'; c++) {
			if (*c < 0x20 || *c > 0x7e) {
				return bad(doc, -1, "bacnet.password");
			}
		}
		(void)uc_strlcpy(out->bacnet_password, d->bacnet.password,
				 sizeof(out->bacnet_password));
	}

	/* log */
	if (d->log.level[0] != '\0') {
		size_t i;

		for (i = 0; i < ARRAY_SIZE(log_levels); i++) {
			if (strcmp(d->log.level, log_levels[i].name) == 0) {
				out->log_level = log_levels[i].level;
				break;
			}
		}
		if (i == ARRAY_SIZE(log_levels)) {
			return bad(doc, -1, "log.level");
		}
	}

	return 0;
}

int uc_config_parse_device(char *json, size_t len, struct uc_device_cfg *out)
{
	struct jd_doc *d;
	int64_t ret;
	int rc;

	if (json == NULL || out == NULL) {
		return -EINVAL;
	}

	uc_config_device_defaults(out);

	d = k_calloc(1, sizeof(*d));
	if (d == NULL) {
		return -ENOMEM;
	}
	d->network.dhcp = true;

	ret = json_obj_parse(json, len, jd_doc_descr, ARRAY_SIZE(jd_doc_descr), d);
	if (ret < 0) {
		rc = map_parse_error(ret, "device.json");
	} else {
		rc = device_from_json(d, (uint64_t)ret, out);
	}

	k_free(d);
	if (rc < 0) {
		uc_config_device_defaults(out);
	}
	return rc;
}

/* ---------------------------------------------------------------------- */
/* io.json                                                                 */
/* ---------------------------------------------------------------------- */

struct ji_point {
	char channel[UC_CHANNEL_NAME_MAX];
	char type[24];
	struct json_obj_token instance;
	char name[UC_NAME_MAX];
	char description[UC_NAME_MAX];
	char units[48];
	struct json_obj_token scale;
	struct json_obj_token offset;
	struct json_obj_token min;
	struct json_obj_token max;
	struct json_obj_token cov_increment;
	struct json_obj_token sample_ms;
	struct json_obj_token debounce_ms;
	bool invert;
};

struct ji_doc {
	struct json_obj_token schema;
	struct ji_point points[CONFIG_UC_IO_POINTS_MAX];
	size_t points_len;
};

static const struct json_obj_descr ji_point_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct ji_point, channel, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct ji_point, type, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct ji_point, instance, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ji_point, name, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct ji_point, description, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct ji_point, units, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct ji_point, scale, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ji_point, offset, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ji_point, min, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ji_point, max, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ji_point, cov_increment, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ji_point, sample_ms, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ji_point, debounce_ms, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ji_point, invert, JSON_TOK_TRUE),
};

static const struct json_obj_descr ji_doc_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct ji_doc, schema, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_OBJ_ARRAY(struct ji_doc, points, CONFIG_UC_IO_POINTS_MAX, points_len,
				 ji_point_descr, ARRAY_SIZE(ji_point_descr)),
};

#define JI_HAS_SCHEMA BIT(0)
#define JI_HAS_POINTS BIT(1)

/* io.schema.json "type" enum */
static bool io_type_allowed(uint16_t t)
{
	switch (t) {
	case OBJECT_ANALOG_INPUT:
	case OBJECT_ANALOG_OUTPUT:
	case OBJECT_ANALOG_VALUE:
	case OBJECT_BINARY_INPUT:
	case OBJECT_BINARY_OUTPUT:
	case OBJECT_BINARY_VALUE:
	case OBJECT_MULTI_STATE_INPUT:
		return true;
	default:
		return false;
	}
}

static int io_point_from_json(const struct ji_point *p, int idx, struct uc_io_point_cfg *o)
{
	static const char doc[] = "io.json";
	double v;
	int rc;

	uc_config_io_point_defaults(o);

	if (p->channel[0] == '\0') {
		return bad(doc, idx, "points.channel");
	}
	(void)uc_strlcpy(o->channel, p->channel, sizeof(o->channel));

	/* text names only (the schema enumerates them) */
	if (p->type[0] < 'a' || uc_obj_type_from_str(p->type, &o->object_type) < 0 ||
	    !io_type_allowed(o->object_type)) {
		return bad(doc, idx, "points.type");
	}

	REQ_INT(&p->instance, 0, UC_INSTANCE_MAX, o->object_instance, doc, idx,
		"points.instance");

	(void)uc_strlcpy(o->name, (p->name[0] != '\0') ? p->name : p->channel, sizeof(o->name));
	(void)uc_strlcpy(o->description, p->description, sizeof(o->description));

	if (p->units[0] != '\0' && uc_units_from_str(p->units, &o->units) < 0) {
		return bad(doc, idx, "points.units");
	}

	rc = tok_num(&p->scale, &v);
	if (rc == 0) {
		o->scale = v;
	} else if (rc != -ENOENT) {
		return bad(doc, idx, "points.scale");
	}

	rc = tok_num(&p->offset, &v);
	if (rc == 0) {
		o->offset = v;
	} else if (rc != -ENOENT) {
		return bad(doc, idx, "points.offset");
	}

	rc = tok_num(&p->min, &v);
	if (rc == 0) {
		o->has_min = true;
		o->min = v;
	} else if (rc != -ENOENT) {
		return bad(doc, idx, "points.min");
	}

	rc = tok_num(&p->max, &v);
	if (rc == 0) {
		o->has_max = true;
		o->max = v;
	} else if (rc != -ENOENT) {
		return bad(doc, idx, "points.max");
	}

	if (o->has_min && o->has_max && o->min > o->max) {
		return bad(doc, idx, "points.min/max");
	}

	rc = tok_num(&p->cov_increment, &v);
	if (rc == 0 && v >= 0.0) {
		o->cov_increment = v;
	} else if (rc != -ENOENT) {
		return bad(doc, idx, "points.cov_increment");
	}

	OPT_INT(&p->sample_ms, 10, 3600000, o->sample_ms, doc, idx, "points.sample_ms");
	OPT_INT(&p->debounce_ms, 0, 10000, o->debounce_ms, doc, idx, "points.debounce_ms");
	o->invert = p->invert;

	return 0;
}

int uc_config_parse_io(char *json, size_t len, struct uc_io_cfg *out)
{
	struct ji_doc *d;
	int64_t ret;
	int rc = 0;

	if (json == NULL || out == NULL) {
		return -EINVAL;
	}

	out->count = 0;

	d = k_calloc(1, sizeof(*d));
	if (d == NULL) {
		return -ENOMEM;
	}

	ret = json_obj_parse(json, len, ji_doc_descr, ARRAY_SIZE(ji_doc_descr), d);
	if (ret < 0) {
		rc = map_parse_error(ret, "io.json");
		goto out;
	}

	if (!schema_ok((uint64_t)ret, JI_HAS_SCHEMA, &d->schema)) {
		rc = bad("io.json", -1, "schema");
		goto out;
	}
	if (((uint64_t)ret & JI_HAS_POINTS) == 0U) {
		rc = bad("io.json", -1, "points");
		goto out;
	}

	for (size_t i = 0; i < d->points_len; i++) {
		rc = io_point_from_json(&d->points[i], (int)i, &out->points[i]);
		if (rc < 0) {
			goto out;
		}
		for (size_t j = 0; j < i; j++) {
			if (out->points[j].object_type == out->points[i].object_type &&
			    out->points[j].object_instance == out->points[i].object_instance) {
				rc = bad("io.json", (int)i, "points (duplicate object)");
				goto out;
			}
		}
	}
	out->count = d->points_len;

out:
	k_free(d);
	if (rc < 0) {
		out->count = 0;
	}
	return rc;
}

/* ---------------------------------------------------------------------- */
/* apps.json                                                               */
/* ---------------------------------------------------------------------- */

#define JA_PERMS_MAX 4

struct ja_param {
	char key[sizeof(((struct uc_app_param *)0)->key)];
	char value[sizeof(((struct uc_app_param *)0)->value)];
};

struct ja_app {
	char name[UC_APP_NAME_MAX];
	char file[UC_PATH_MAX];
	bool autostart;
	struct json_obj_token period_ms;
	struct json_obj_token heap_kb;
	struct json_obj_token stack_kb;
	char perms[JA_PERMS_MAX][16];
	size_t perms_len;
	struct ja_param params[CONFIG_UC_APP_PARAMS_MAX];
	size_t params_len;
	char sha256[65];
};

struct ja_doc {
	struct json_obj_token schema;
	struct ja_app apps[CONFIG_UC_APPS_MAX];
	size_t apps_len;
};

static const struct json_obj_descr ja_param_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct ja_param, key, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct ja_param, value, JSON_TOK_STRING_BUF),
};

static const struct json_obj_descr ja_app_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct ja_app, name, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct ja_app, file, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_PRIM(struct ja_app, autostart, JSON_TOK_TRUE),
	JSON_OBJ_DESCR_PRIM(struct ja_app, period_ms, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ja_app, heap_kb, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_PRIM(struct ja_app, stack_kb, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_ARRAY(struct ja_app, perms, JA_PERMS_MAX, perms_len, JSON_TOK_STRING_BUF),
	JSON_OBJ_DESCR_OBJ_ARRAY(struct ja_app, params, CONFIG_UC_APP_PARAMS_MAX, params_len,
				 ja_param_descr, ARRAY_SIZE(ja_param_descr)),
	JSON_OBJ_DESCR_PRIM(struct ja_app, sha256, JSON_TOK_STRING_BUF),
};

static const struct json_obj_descr ja_doc_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct ja_doc, schema, JSON_TOK_FLOAT),
	JSON_OBJ_DESCR_OBJ_ARRAY(struct ja_doc, apps, CONFIG_UC_APPS_MAX, apps_len, ja_app_descr,
				 ARRAY_SIZE(ja_app_descr)),
};

#define JA_HAS_SCHEMA BIT(0)
#define JA_HAS_APPS   BIT(1)

/* Semantic checks shared by the parser and uc_config_set_apps(). */
static int app_cfg_validate(const struct uc_app_cfg *a, int idx)
{
	static const char doc[] = "apps.json";
	const uint32_t all_perms =
		UC_PERM_BACNET_LOCAL | UC_PERM_BACNET_REMOTE | UC_PERM_IO | UC_PERM_KV;

	if (!str_fits(a->name, sizeof(a->name)) || !uc_app_name_valid(a->name)) {
		return bad(doc, idx, "apps.name");
	}
	if (!str_fits(a->file, sizeof(a->file)) || !app_file_valid(a->file)) {
		return bad(doc, idx, "apps.file");
	}
	if (a->period_ms > 3600000U) {
		return bad(doc, idx, "apps.period_ms");
	}
	if (a->heap_kb > 256U) {
		return bad(doc, idx, "apps.heap_kb");
	}
	if (a->stack_kb < 1U || a->stack_kb > 64U) {
		return bad(doc, idx, "apps.stack_kb");
	}
	if ((a->perms & ~all_perms) != 0U) {
		return bad(doc, idx, "apps.perms");
	}
	if (a->param_count > CONFIG_UC_APP_PARAMS_MAX) {
		return bad(doc, idx, "apps.params");
	}
	for (size_t k = 0; k < a->param_count; k++) {
		const struct uc_app_param *p = &a->params[k];

		if (!str_fits(p->key, sizeof(p->key)) || !uc_key_valid(p->key, UC_PARAM_KEY_MAX) ||
		    !str_fits(p->value, sizeof(p->value))) {
			return bad(doc, idx, "apps.params");
		}
	}
	return 0;
}

static int apps_cfg_validate(const struct uc_apps_cfg *cfg)
{
	int rc;

	if (cfg->count > CONFIG_UC_APPS_MAX) {
		return -ENOSPC;
	}
	for (size_t i = 0; i < cfg->count; i++) {
		rc = app_cfg_validate(&cfg->apps[i], (int)i);
		if (rc < 0) {
			return rc;
		}
		for (size_t j = 0; j < i; j++) {
			if (strcmp(cfg->apps[j].name, cfg->apps[i].name) == 0) {
				return bad("apps.json", (int)i, "apps.name (duplicate)");
			}
		}
	}
	return 0;
}

static int app_from_json(const struct ja_app *a, int idx, struct uc_app_cfg *o)
{
	static const char doc[] = "apps.json";

	uc_config_app_defaults(o);

	if (!uc_app_name_valid(a->name)) {
		return bad(doc, idx, "apps.name");
	}
	(void)uc_strlcpy(o->name, a->name, sizeof(o->name));
	if (!app_file_valid(a->file)) {
		return bad(doc, idx, "apps.file");
	}
	(void)uc_strlcpy(o->file, a->file, sizeof(o->file));

	o->autostart = a->autostart;
	OPT_INT(&a->period_ms, 0, 3600000, o->period_ms, doc, idx, "apps.period_ms");
	OPT_INT(&a->heap_kb, 0, 256, o->heap_kb, doc, idx, "apps.heap_kb");
	OPT_INT(&a->stack_kb, 1, 64, o->stack_kb, doc, idx, "apps.stack_kb");

	for (size_t k = 0; k < a->perms_len; k++) {
		uint32_t bit = uc_perm_from_str(a->perms[k]);

		if (bit == 0U || (o->perms & bit) != 0U) {
			return bad(doc, idx, "apps.perms");
		}
		o->perms |= bit;
	}

	for (size_t k = 0; k < a->params_len; k++) {
		const struct ja_param *p = &a->params[k];

		if (!uc_key_valid(p->key, UC_PARAM_KEY_MAX)) {
			return bad(doc, idx, "apps.params.key");
		}
		(void)uc_strlcpy(o->params[k].key, p->key, sizeof(o->params[k].key));
		(void)uc_strlcpy(o->params[k].value, p->value, sizeof(o->params[k].value));
	}
	o->param_count = a->params_len;

	if (a->sha256[0] != '\0') {
		if (!sha256_parse(a->sha256, o->sha256)) {
			return bad(doc, idx, "apps.sha256");
		}
		o->has_sha256 = true;
	}

	return 0;
}

int uc_config_parse_apps(char *json, size_t len, struct uc_apps_cfg *out)
{
	struct ja_doc *d;
	int64_t ret;
	int rc = 0;

	if (json == NULL || out == NULL) {
		return -EINVAL;
	}

	out->count = 0;

	d = k_calloc(1, sizeof(*d));
	if (d == NULL) {
		return -ENOMEM;
	}
	for (size_t i = 0; i < ARRAY_SIZE(d->apps); i++) {
		d->apps[i].autostart = true;
	}

	ret = json_obj_parse(json, len, ja_doc_descr, ARRAY_SIZE(ja_doc_descr), d);
	if (ret < 0) {
		rc = map_parse_error(ret, "apps.json");
		goto out;
	}

	if (!schema_ok((uint64_t)ret, JA_HAS_SCHEMA, &d->schema)) {
		rc = bad("apps.json", -1, "schema");
		goto out;
	}
	if (((uint64_t)ret & JA_HAS_APPS) == 0U) {
		rc = bad("apps.json", -1, "apps");
		goto out;
	}

	for (size_t i = 0; i < d->apps_len; i++) {
		rc = app_from_json(&d->apps[i], (int)i, &out->apps[i]);
		if (rc < 0) {
			goto out;
		}
	}
	out->count = d->apps_len;
	rc = apps_cfg_validate(out);

out:
	k_free(d);
	if (rc < 0) {
		out->count = 0;
	}
	return rc;
}

/* ---------------------------------------------------------------------- */
/* apps.json encoder                                                       */
/* ---------------------------------------------------------------------- */

struct jw {
	char *buf;
	size_t size;
	size_t len;
	bool overflow;
};

static void jw_putc(struct jw *w, char c)
{
	if (w->len + 1 < w->size) {
		w->buf[w->len++] = c;
	} else {
		w->overflow = true;
	}
}

static void jw_puts(struct jw *w, const char *s)
{
	while (*s != '\0') {
		jw_putc(w, *s++);
	}
}

static void jw_str(struct jw *w, const char *s)
{
	jw_putc(w, '"');
	for (; *s != '\0'; s++) {
		unsigned char c = (unsigned char)*s;

		switch (c) {
		case '"':
			jw_puts(w, "\\\"");
			break;
		case '\\':
			jw_puts(w, "\\\\");
			break;
		case '\n':
			jw_puts(w, "\\n");
			break;
		case '\r':
			jw_puts(w, "\\r");
			break;
		case '\t':
			jw_puts(w, "\\t");
			break;
		case '\b':
			jw_puts(w, "\\b");
			break;
		case '\f':
			jw_puts(w, "\\f");
			break;
		default:
			if (c < 0x20U) {
				char esc[8];

				snprintk(esc, sizeof(esc), "\\u%04x", c);
				jw_puts(w, esc);
			} else {
				jw_putc(w, (char)c);
			}
			break;
		}
	}
	jw_putc(w, '"');
}

static void jw_uint(struct jw *w, uint32_t v)
{
	char num[12];

	snprintk(num, sizeof(num), "%u", v);
	jw_puts(w, num);
}

static void jw_key(struct jw *w, const char *key)
{
	jw_str(w, key);
	jw_putc(w, ':');
}

int uc_config_encode_apps(const struct uc_apps_cfg *cfg, char *buf, size_t buf_len)
{
	static const char hex[] = "0123456789abcdef";
	struct jw w = {.buf = buf, .size = buf_len};

	if (cfg == NULL || buf == NULL || buf_len == 0) {
		return -EINVAL;
	}

	jw_puts(&w, "{\"schema\":1,\"apps\":[");
	for (size_t i = 0; i < cfg->count && i < CONFIG_UC_APPS_MAX; i++) {
		const struct uc_app_cfg *a = &cfg->apps[i];
		bool first = true;

		jw_puts(&w, (i == 0) ? "\n{" : ",\n{");
		jw_key(&w, "name");
		jw_str(&w, a->name);
		jw_putc(&w, ',');
		jw_key(&w, "file");
		jw_str(&w, a->file);
		jw_putc(&w, ',');
		jw_key(&w, "autostart");
		jw_puts(&w, a->autostart ? "true" : "false");
		jw_putc(&w, ',');
		jw_key(&w, "period_ms");
		jw_uint(&w, a->period_ms);
		jw_putc(&w, ',');
		jw_key(&w, "heap_kb");
		jw_uint(&w, a->heap_kb);
		jw_putc(&w, ',');
		jw_key(&w, "stack_kb");
		jw_uint(&w, a->stack_kb);
		jw_putc(&w, ',');

		jw_key(&w, "perms");
		jw_putc(&w, '[');
		for (uint32_t bit = BIT(0); bit <= UC_PERM_KV; bit <<= 1) {
			if ((a->perms & bit) == 0U || uc_perm_to_str(bit) == NULL) {
				continue;
			}
			if (!first) {
				jw_putc(&w, ',');
			}
			jw_str(&w, uc_perm_to_str(bit));
			first = false;
		}
		jw_puts(&w, "],");

		jw_key(&w, "params");
		jw_putc(&w, '[');
		for (size_t k = 0; k < a->param_count && k < CONFIG_UC_APP_PARAMS_MAX; k++) {
			if (k > 0) {
				jw_putc(&w, ',');
			}
			jw_putc(&w, '{');
			jw_key(&w, "key");
			jw_str(&w, a->params[k].key);
			jw_putc(&w, ',');
			jw_key(&w, "value");
			jw_str(&w, a->params[k].value);
			jw_putc(&w, '}');
		}
		jw_putc(&w, ']');

		if (a->has_sha256) {
			jw_putc(&w, ',');
			jw_key(&w, "sha256");
			jw_putc(&w, '"');
			for (size_t k = 0; k < sizeof(a->sha256); k++) {
				jw_putc(&w, hex[a->sha256[k] >> 4]);
				jw_putc(&w, hex[a->sha256[k] & 0x0f]);
			}
			jw_putc(&w, '"');
		}
		jw_putc(&w, '}');
	}
	jw_puts(&w, "\n]}\n");

	if (w.overflow) {
		buf[0] = '\0';
		return -ENOSPC;
	}

	buf[w.len] = '\0';
	return (int)w.len;
}

/* ---------------------------------------------------------------------- */
/* Cache                                                                   */
/* ---------------------------------------------------------------------- */

static K_MUTEX_DEFINE(cfg_lock);  /* protects the cache */
/* serialises document loads (shell and SMP may reload at the same time; a
 * staged document must be validated and renamed by one of them only)
 */
static K_MUTEX_DEFINE(load_lock);
static K_MUTEX_DEFINE(apps_lock); /* serialises uc_config_set_apps() */

static struct uc_device_cfg cache_device;
static struct uc_io_cfg cache_io;
static struct uc_apps_cfg cache_apps;
static bool cache_valid;

static void cache_defaults_locked(void)
{
	if (!cache_valid) {
		uc_config_device_defaults(&cache_device);
		memset(&cache_io, 0, sizeof(cache_io));
		memset(&cache_apps, 0, sizeof(cache_apps));
		cache_valid = true;
	}
}

static int parse_device_any(char *json, size_t len, void *out)
{
	return uc_config_parse_device(json, len, out);
}

static int parse_io_any(char *json, size_t len, void *out)
{
	return uc_config_parse_io(json, len, out);
}

static int parse_apps_any(char *json, size_t len, void *out)
{
	return uc_config_parse_apps(json, len, out);
}

static void defaults_device_any(void *out)
{
	uc_config_device_defaults(out);
}

static void defaults_io_any(void *out)
{
	((struct uc_io_cfg *)out)->count = 0;
}

static void defaults_apps_any(void *out)
{
	((struct uc_apps_cfg *)out)->count = 0;
}

struct cfg_doc {
	uint32_t bit;
	const char *path;
	void *cache;
	size_t size;
	int (*parse)(char *json, size_t len, void *out);
	void (*defaults)(void *out);
};

static const struct cfg_doc cfg_docs[] = {
	{UC_CFG_DEVICE, UC_FILE_DEVICE_CFG, &cache_device, sizeof(cache_device),
	 parse_device_any, defaults_device_any},
	{UC_CFG_IO, UC_FILE_IO_CFG, &cache_io, sizeof(cache_io), parse_io_any,
	 defaults_io_any},
	{UC_CFG_APPS, UC_FILE_APPS_CFG, &cache_apps, sizeof(cache_apps), parse_apps_any,
	 defaults_apps_any},
};

/* Read and parse one file into out: 0, -ENOENT (missing), a parser error
 * (-EINVAL, -ENOSPC) or a storage error (-ENODEV: /lfs not mounted, -EFBIG:
 * larger than CONFIG_UC_CONFIG_DOC_MAX, -EIO, -ENOMEM, ...).
 */
static int read_doc(const struct cfg_doc *doc, const char *path, void *out)
{
	char *json = NULL;
	size_t len = 0;
	int rc;

	if (!uc_storage_ready()) {
		/* no /lfs: not "missing" (a reload must keep the cache) */
		return -ENODEV;
	}

	rc = uc_storage_read_file(path, &json, &len, CONFIG_UC_CONFIG_DOC_MAX);
	if (rc < 0) {
		return rc;
	}

	rc = doc->parse(json, len, out);
	k_free(json);
	return rc;
}

/*
 * Staged document <path>.new: 1 when it was valid and has been renamed over
 * <path> (out holds it), 0 when there is none, -EINVAL when it was rejected
 * and deleted, another negative errno when it could not be read or renamed
 * (then it is left in place for another attempt).
 */
static int load_staged(const struct cfg_doc *doc, void *out)
{
	char staged[UC_PATH_MAX];
	int rc;

	if (!uc_storage_ready()) {
		return 0;
	}
	if (snprintk(staged, sizeof(staged), "%s" UC_CFG_STAGED_SUFFIX, doc->path) >=
	    (int)sizeof(staged)) {
		return -ENAMETOOLONG;
	}

	rc = read_doc(doc, staged, out);
	if (rc == -ENOENT) {
		return 0;
	}
	if (rc == -EINVAL || rc == -ENOSPC || rc == -EFBIG) {
		LOG_WRN("%s rejected (%d), deleting it", staged, rc);
		rc = uc_storage_remove(staged);
		if (rc < 0) {
			LOG_ERR("%s: delete failed (%d)", staged, rc);
		}
		return -EINVAL;
	}
	if (rc < 0) {
		LOG_WRN("%s: read failed (%d)", staged, rc);
		return rc;
	}

	rc = uc_storage_rename(staged, doc->path);
	if (rc < 0) {
		LOG_ERR("%s: activation failed (%d)", staged, rc);
		return rc;
	}

	LOG_INF("%s activated", staged);
	return 1;
}

/*
 * Load one document into the cache: the staged <path>.new if there is one,
 * else <path>. A missing <path> yields defaults. On an error the cache keeps
 * its content when keep_on_error is set (reload), otherwise it gets defaults
 * (boot; a rejected staged document falls back to <path> there).
 */
static int load_doc_locked(const struct cfg_doc *doc, bool keep_on_error)
{
	void *tmp;
	int rc;

	tmp = k_malloc(doc->size);
	if (tmp == NULL) {
		LOG_ERR("%s: out of memory", doc->path);
		return -ENOMEM;
	}

	rc = load_staged(doc, tmp);
	if (rc > 0) {
		rc = 0;
		goto commit;
	}
	if (rc < 0 && keep_on_error) {
		LOG_WRN("%s: keeping the active configuration", doc->path);
		k_free(tmp);
		return rc;
	}

	rc = read_doc(doc, doc->path, tmp);
	if (rc == -ENOENT) {
		LOG_INF("%s not found, using defaults", doc->path);
		doc->defaults(tmp);
		rc = 0;
	} else if (rc == 0) {
		LOG_INF("%s loaded", doc->path);
	}

	if (rc < 0) {
		if (keep_on_error) {
			LOG_WRN("%s rejected (%d), keeping the active configuration", doc->path,
				rc);
			k_free(tmp);
			return rc;
		}
		LOG_WRN("%s rejected (%d), using defaults", doc->path, rc);
		doc->defaults(tmp);
	}

commit:
	k_mutex_lock(&cfg_lock, K_FOREVER);
	cache_defaults_locked();
	memcpy(doc->cache, tmp, doc->size);
	k_mutex_unlock(&cfg_lock);

	k_free(tmp);
	return rc;
}

static int load_doc(const struct cfg_doc *doc, bool keep_on_error)
{
	int rc;

	k_mutex_lock(&load_lock, K_FOREVER);
	rc = load_doc_locked(doc, keep_on_error);
	k_mutex_unlock(&load_lock);

	return rc;
}

static const char *log_level_name(uint8_t level)
{
	static const char *const names[] = {"none", "err", "wrn", "inf", "dbg"};

	return (level < ARRAY_SIZE(names)) ? names[level] : "?";
}

void uc_config_apply_log_level(void)
{
	uint8_t level;

	k_mutex_lock(&cfg_lock, K_FOREVER);
	cache_defaults_locked();
	level = cache_device.log_level;
	k_mutex_unlock(&cfg_lock);

#if defined(CONFIG_LOG_RUNTIME_FILTERING)
	uint32_t count = log_src_cnt_get(Z_LOG_LOCAL_DOMAIN_ID);

	for (uint32_t i = 0; i < count; i++) {
		(void)log_filter_set(NULL, Z_LOG_LOCAL_DOMAIN_ID, (int16_t)i, level);
	}
	LOG_INF("log level %s (%u sources)", log_level_name(level), count);
#else
	LOG_WRN("CONFIG_LOG_RUNTIME_FILTERING disabled, log level %s not applied",
		log_level_name(level));
#endif
}

int uc_config_init(void)
{
	int first_err = 0;

	k_mutex_lock(&cfg_lock, K_FOREVER);
	cache_defaults_locked();
	k_mutex_unlock(&cfg_lock);

	if (!uc_storage_ready()) {
		LOG_WRN("storage not ready, using default configuration");
	}

	for (size_t i = 0; i < ARRAY_SIZE(cfg_docs); i++) {
		int rc = load_doc(&cfg_docs[i], false);

		if (rc < 0 && first_err == 0) {
			first_err = rc;
		}
	}

	uc_config_apply_log_level();

	return first_err;
}

int uc_config_reload(uint32_t docs)
{
	int first_err = 0;

	if ((docs & UC_CFG_ALL) == 0U || (docs & ~(uint32_t)UC_CFG_ALL) != 0U) {
		return -EINVAL;
	}

	for (size_t i = 0; i < ARRAY_SIZE(cfg_docs); i++) {
		int rc;

		if ((docs & cfg_docs[i].bit) == 0U) {
			continue;
		}
		rc = load_doc(&cfg_docs[i], true);
		if (rc < 0 && first_err == 0) {
			first_err = rc;
		}
		if (rc == 0 && cfg_docs[i].bit == UC_CFG_DEVICE) {
			uc_config_apply_log_level();
		}
	}

	return first_err;
}

void uc_config_get_device(struct uc_device_cfg *out)
{
	if (out == NULL) {
		return;
	}
	k_mutex_lock(&cfg_lock, K_FOREVER);
	cache_defaults_locked();
	memcpy(out, &cache_device, sizeof(*out));
	k_mutex_unlock(&cfg_lock);
}

void uc_config_get_io(struct uc_io_cfg *out)
{
	if (out == NULL) {
		return;
	}
	k_mutex_lock(&cfg_lock, K_FOREVER);
	cache_defaults_locked();
	memcpy(out, &cache_io, sizeof(*out));
	k_mutex_unlock(&cfg_lock);
}

void uc_config_get_apps(struct uc_apps_cfg *out)
{
	if (out == NULL) {
		return;
	}
	k_mutex_lock(&cfg_lock, K_FOREVER);
	cache_defaults_locked();
	memcpy(out, &cache_apps, sizeof(*out));
	k_mutex_unlock(&cfg_lock);
}

int uc_config_set_apps(const struct uc_apps_cfg *cfg)
{
	char *buf;
	int len;
	int rc;

	if (cfg == NULL) {
		return -EINVAL;
	}

	rc = apps_cfg_validate(cfg);
	if (rc < 0) {
		return rc;
	}

	buf = k_malloc(CONFIG_UC_CONFIG_DOC_MAX + 1);
	if (buf == NULL) {
		return -ENOMEM;
	}

	k_mutex_lock(&apps_lock, K_FOREVER);

	len = uc_config_encode_apps(cfg, buf, CONFIG_UC_CONFIG_DOC_MAX + 1);
	if (len < 0) {
		LOG_ERR("apps.json exceeds %d bytes", CONFIG_UC_CONFIG_DOC_MAX);
		rc = len;
		goto out;
	}

	rc = uc_storage_write_file(UC_FILE_APPS_CFG, buf, (size_t)len);
	if (rc < 0) {
		LOG_ERR("%s: write failed (%d)", UC_FILE_APPS_CFG, rc);
		goto out;
	}

	k_mutex_lock(&cfg_lock, K_FOREVER);
	cache_defaults_locked();
	memcpy(&cache_apps, cfg, sizeof(cache_apps));
	k_mutex_unlock(&cfg_lock);
	LOG_INF("%s written (%u apps)", UC_FILE_APPS_CFG, (unsigned int)cfg->count);

out:
	k_mutex_unlock(&apps_lock);
	k_free(buf);
	return rc;
}

const char *uc_app_cfg_param(const struct uc_app_cfg *cfg, const char *key)
{
	if (cfg == NULL || key == NULL) {
		return NULL;
	}

	for (size_t i = 0; i < cfg->param_count && i < CONFIG_UC_APP_PARAMS_MAX; i++) {
		if (strcmp(cfg->params[i].key, key) == 0) {
			return cfg->params[i].value;
		}
	}

	return NULL;
}

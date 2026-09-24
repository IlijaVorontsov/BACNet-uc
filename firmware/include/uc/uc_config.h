/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Configuration documents /lfs/cfg/{device,io,apps}.json (see schemas/).
 * Parsed with Zephyr's JSON library into the structs below and cached in
 * RAM. A missing or invalid document yields defaults (Kconfig) and a
 * logged warning; the node always boots.
 */
#ifndef UC_CONFIG_H_
#define UC_CONFIG_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/kernel.h>

#include "uc_common.h"

#ifdef __cplusplus
extern "C" {
#endif

struct uc_static_binding {
	uint32_t device;
	char address[16]; /* dotted IPv4 */
	uint16_t port;
};

struct uc_device_cfg {
	uint32_t instance;
	char name[UC_NAME_MAX];
	char description[UC_NAME_MAX];
	char location[UC_NAME_MAX];
	/* network */
	bool dhcp;
	char ipv4[16];
	char netmask[16];
	char gateway[16];
	/* bacnet */
	uint16_t udp_port;
	uint16_t apdu_timeout_ms;
	uint8_t apdu_retries;
	bool fd_enabled;        /* foreign device registration */
	char fd_bbmd[16];
	uint16_t fd_port;
	uint16_t fd_ttl_s;
	size_t binding_count;
	struct uc_static_binding bindings[CONFIG_UC_BACNET_STATIC_BINDINGS_MAX];
	/* log */
	uint8_t log_level;      /* LOG_LEVEL_ERR..LOG_LEVEL_DBG */
};

struct uc_io_point_cfg {
	char channel[UC_CHANNEL_NAME_MAX];
	uint16_t object_type;
	uint32_t object_instance;
	char name[UC_NAME_MAX];
	char description[UC_NAME_MAX];
	uint16_t units;         /* BACNET_ENGINEERING_UNITS, UNITS_NO_UNITS default */
	double scale;           /* default 1.0 */
	double offset;          /* default 0.0 */
	bool has_min, has_max;
	double min, max;
	double cov_increment;   /* default 0.1 */
	uint32_t sample_ms;     /* default 100 */
	uint32_t debounce_ms;   /* default 20 */
	bool invert;
};

struct uc_io_cfg {
	size_t count;
	struct uc_io_point_cfg points[CONFIG_UC_IO_POINTS_MAX];
};

struct uc_app_param {
	char key[24];
	char value[96];
};

struct uc_app_cfg {
	char name[UC_APP_NAME_MAX];
	char file[UC_PATH_MAX];
	bool autostart;         /* default true */
	uint32_t period_ms;     /* default 1000 */
	uint16_t heap_kb;       /* default 8 */
	uint16_t stack_kb;      /* default 4 */
	uint32_t perms;         /* UC_PERM_* bitmask */
	size_t param_count;
	struct uc_app_param params[CONFIG_UC_APP_PARAMS_MAX];
	bool has_sha256;
	uint8_t sha256[32];
};

struct uc_apps_cfg {
	size_t count;
	struct uc_app_cfg apps[CONFIG_UC_APPS_MAX];
};

enum uc_cfg_doc {
	UC_CFG_DEVICE = BIT(0),
	UC_CFG_IO = BIT(1),
	UC_CFG_APPS = BIT(2),
	UC_CFG_ALL = UC_CFG_DEVICE | UC_CFG_IO | UC_CFG_APPS,
};

/* Defaults (Kconfig based). */
void uc_config_device_defaults(struct uc_device_cfg *cfg);
void uc_config_app_defaults(struct uc_app_cfg *cfg);
void uc_config_io_point_defaults(struct uc_io_point_cfg *pt);

/* Pure parsers/encoders (no file access, unit tested). The parsers modify
 * json in place (Zephyr JSON library) and fill *out completely, applying
 * defaults for absent optional fields. They return 0, -EINVAL for syntax
 * or schema errors, -ENOSPC when a table limit is exceeded. */
int uc_config_parse_device(char *json, size_t len, struct uc_device_cfg *out);
int uc_config_parse_io(char *json, size_t len, struct uc_io_cfg *out);
int uc_config_parse_apps(char *json, size_t len, struct uc_apps_cfg *out);
/** Serialize to JSON (apps.json format, params as [{"key","value"}]).
 *  Returns the length written (without NUL) or -ENOSPC. */
int uc_config_encode_apps(const struct uc_apps_cfg *cfg, char *buf,
			  size_t buf_len);

/** Load all documents from /lfs/cfg into the cache (defaults on error). */
int uc_config_init(void);

/** Re-read the given documents (bitmask of enum uc_cfg_doc) from disk. */
int uc_config_reload(uint32_t docs);

/* Thread-safe copies of the cached configuration. */
void uc_config_get_device(struct uc_device_cfg *out);
void uc_config_get_io(struct uc_io_cfg *out);
void uc_config_get_apps(struct uc_apps_cfg *out);

/** Replace the cached apps configuration and persist apps.json. */
int uc_config_set_apps(const struct uc_apps_cfg *cfg);

/** Helper: look up a parameter of an app configuration, NULL if absent. */
const char *uc_app_cfg_param(const struct uc_app_cfg *cfg, const char *key);

#ifdef __cplusplus
}
#endif

#endif /* UC_CONFIG_H_ */

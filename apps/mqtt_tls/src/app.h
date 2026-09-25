/*
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef APP_H_
#define APP_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <zephyr/kernel.h>
#include <zephyr/net/tls_credentials.h>

/* config.c ------------------------------------------------------------------ */

/** Longest string setting, including the terminating NUL. */
#define APP_CFG_STR_LEN 64

/** Runtime configuration: Kconfig defaults overlaid with stored settings. */
struct app_config {
	char broker_host[APP_CFG_STR_LEN];
	char tls_hostname[APP_CFG_STR_LEN]; /* empty: use broker_host */
	char username[APP_CFG_STR_LEN];
	char password[APP_CFG_STR_LEN];
	char topic_root[APP_CFG_STR_LEN];
	uint32_t publish_interval; /* seconds */
	uint16_t broker_port;
	uint16_t keepalive;        /* seconds */
	uint8_t log_level;         /* LOG_LEVEL_* threshold for the MQTT log topic */
};

/** Load stored settings on top of the Kconfig defaults. */
int app_config_init(void);

/** Snapshot of the current configuration. */
void app_config_get(struct app_config *out);

/** Snapshot plus the mask of keys that are stored (not Kconfig defaults). */
void app_config_snapshot(struct app_config *out, uint32_t *mask);

/**
 * Validate, store and apply one key. @p next_connect tells whether the change
 * takes effect only on the next connection. Returns 0, -ENOENT (unknown key),
 * -EINVAL or -ENAMETOOLONG (bad value) or -EIO (storage failed). A new
 * log_level is applied immediately.
 */
int app_config_set(const char *name, const char *value, bool *next_connect);

/** Current value of one key as text; secrets read as "***". */
int app_config_get_value(const char *name, char *buf, size_t len);

/** Delete all stored settings: back to the Kconfig defaults (applied now). */
int app_config_reset(void);

/** The whole configuration as a JSON object (secrets masked). Length or -ENOMEM. */
int app_config_json(char *buf, size_t len);

/** Comma-separated JSON strings of all key names, for caps.config. */
void app_config_key_list(char *buf, size_t len);

/**
 * Call before each connection attempt. Counts down a trial of new connection
 * settings; returns true if it just fell back to the last known good.
 */
bool app_config_attempt(void);

/** topic_root of the last known good configuration, or "" if there is none. */
void app_config_lkg_topic_root(char *buf, size_t len);

/**
 * Call when a session reached "online". @p used (with its stored-key mask
 * @p used_mask, both from app_config_snapshot) is the configuration that
 * session connected with; it becomes the last known good.
 */
void app_config_online(const struct app_config *used, uint32_t used_mask);

/* net_wait.c ---------------------------------------------------------------- */

/** Register for connectivity events and start address configuration. */
void app_net_init(void);

/** True while the default interface is up and has an IPv4 address. */
bool app_net_is_up(void);

/** Block until the network is up or @p timeout expires. 0 or -EAGAIN. */
int app_net_wait_up(k_timeout_t timeout);

/* device_id.c --------------------------------------------------------------- */

/**
 * Write the MQTT client identifier into @p buf (NUL-terminated).
 * Returns 0, or -ENOSPC if @p len is too small.
 */
int app_client_id_get(char *buf, size_t len);

/** Write the MCU unique ID in hex (NUL-terminated), or "unknown". */
void app_hwid_get(char *buf, size_t len);

/* tls_creds.c --------------------------------------------------------------- */

/** Security tag under which all of the application's credentials live. */
#define APP_TLS_SEC_TAG 0x4d5154 /* "MQT" */

/** Check and register the embedded CA (and client) credentials. */
int app_tls_creds_register(void);

/* watchdog.c ---------------------------------------------------------------- */

#if defined(CONFIG_APP_WATCHDOG)
/** Arm the task watchdog channel for the main loop. */
int app_wdt_init(void);

/** Tell the watchdog that the main loop is making progress. */
void app_wdt_feed(void);
#else
static inline int app_wdt_init(void)
{
	return 0;
}

static inline void app_wdt_feed(void)
{
}
#endif

/* mqtt_app.c ---------------------------------------------------------------- */

/** One-time client setup (topics, client id). */
int app_mqtt_init(void);

/**
 * Run one MQTT session: resolve, connect, serve until the connection is lost.
 *
 * @param[out] was_connected set to true if the broker accepted the session
 *             and it stayed up long enough to count as healthy, so the
 *             caller can reset its back-off.
 * @return negative errno describing why the session ended.
 */
int app_mqtt_run_session(bool *was_connected);

/* log_mqtt.c ---------------------------------------------------------------- */

/** One log line captured for the MQTT log topic. */
struct app_log_line {
	uint32_t t_ms;   /* uptime */
	uint8_t level;   /* LOG_LEVEL_ERR .. LOG_LEVEL_DBG */
	char src[20];    /* log module */
	char msg[120];
};

/** Threshold for lines captured by the MQTT log backend (LOG_LEVEL_*). */
void app_log_set_level(uint8_t level);

/**
 * Fetch the next line after @p cursor (a running line number, 0 = first ever).
 * @p lost is set to the number of lines that fell out of the ring before they
 * were read. Returns false when there is nothing new.
 */
bool app_log_next(uint32_t *cursor, struct app_log_line *out, uint32_t *lost);

/** Number of lines captured since boot. */
uint32_t app_log_count(void);

const char *app_log_level_name(uint8_t level);

/** One line as a JSON object. Length, or -ENOMEM. */
int app_log_line_json(const struct app_log_line *line, uint32_t lost, char *buf, size_t len);

/** Copy @p src into @p dst as the body of a JSON string. Returns the length. */
size_t app_json_escape(char *dst, size_t len, const char *src);

/* mgmt.c -------------------------------------------------------------------- */

/**
 * Note whether this is an unconfirmed MCUboot test image; if so, schedule the
 * revert reboot (cancelled by app_mgmt_online).
 */
void app_mgmt_init(void);

/** The device reached "online": confirm a test image. */
void app_mgmt_online(void);

/** The "mgmt" and "boot" members of the info message (no braces). */
int app_mgmt_info_json(char *buf, size_t len);

/* mqtt_app.c (continued) ------------------------------------------------------ */

/** End the current session after the pending reply; reconnect with new settings. */
void app_mqtt_request_reconnect(void);

/* (log_mqtt.c) The last @p n log lines as a JSON array member
 * "logs":[...] into @p buf, as many as fit. Returns the number of lines.
 */
int app_log_last_json(size_t n, char *buf, size_t len);

/* commands.c ---------------------------------------------------------------- */

/** Set up the LED used by the led and identify commands. */
void app_commands_init(void);

/**
 * Execute a command received on the command topic (plain text or JSON) and
 * write the JSON reply. @p payload is NUL-terminated and modified in place.
 */
void app_handle_command(char *payload, char *reply, size_t reply_len);

/** Write the "caps" JSON object announced in the retained info message. */
void app_commands_caps(char *buf, size_t len);

#endif /* APP_H_ */

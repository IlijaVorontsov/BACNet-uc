/*
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef APP_H_
#define APP_H_

#include <stdbool.h>
#include <stddef.h>
#include <zephyr/kernel.h>
#include <zephyr/net/tls_credentials.h>

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

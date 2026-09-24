/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * MQTT over Ethernet + TLS for STM32 and NXP MCX boards.
 *
 * main() waits for the network, runs MQTT sessions back to back and applies
 * an exponential back-off with jitter between failed attempts, so a fleet of
 * devices does not reconnect in lock-step after a broker outage.
 */

#include <errno.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/random/random.h>

#include "app.h"

LOG_MODULE_REGISTER(app, CONFIG_APP_LOG_LEVEL);

BUILD_ASSERT(CONFIG_APP_MQTT_RECONNECT_MIN_MS > 0 &&
	     CONFIG_APP_MQTT_RECONNECT_MIN_MS <= CONFIG_APP_MQTT_RECONNECT_MAX_MS,
	     "invalid reconnect back-off range");

/* Exponential back-off; each delay is randomised to 75..125 % of the base. */
static uint32_t next_backoff(uint32_t *backoff_ms)
{
	uint32_t base = *backoff_ms;
	uint32_t jitter = base / 2U;
	uint32_t delay = base - base / 4U + (jitter ? sys_rand32_get() % jitter : 0U);

	*backoff_ms = MIN(base * 2U, (uint32_t)CONFIG_APP_MQTT_RECONNECT_MAX_MS);

	return delay;
}

/* Sleep without starving the watchdog. */
static void sleep_fed(uint32_t ms)
{
	while (ms > 0U) {
		uint32_t chunk = MIN(ms, 5000U);

		app_wdt_feed();
		k_sleep(K_MSEC(chunk));
		ms -= chunk;
	}
	app_wdt_feed();
}

int main(void)
{
	uint32_t backoff_ms = CONFIG_APP_MQTT_RECONNECT_MIN_MS;
	int ret;

	LOG_INF("MQTT over Ethernet + TLS on %s", CONFIG_BOARD);

	app_commands_init();

	ret = app_mqtt_init();
	if (ret < 0) {
		LOG_ERR("Invalid configuration, MQTT client not started");
		return ret;
	}

#if defined(CONFIG_APP_MQTT_TLS)
	ret = app_tls_creds_register();
	if (ret < 0) {
		LOG_ERR("Invalid TLS credentials, MQTT client not started");
		return ret;
	}
#endif

	/* Armed only once the configuration is known to be usable, so a
	 * misconfigured device logs its error instead of reboot-looping.
	 */
	ret = app_wdt_init();
	if (ret < 0) {
		return ret;
	}

	app_net_init();

	while (true) {
		bool healthy;
		uint32_t delay;

		if (!app_net_is_up()) {
			LOG_INF("Waiting for network...");
			while (app_net_wait_up(K_SECONDS(5)) != 0) {
				app_wdt_feed();
			}
		}

		ret = app_mqtt_run_session(&healthy);
		if (healthy) {
			backoff_ms = CONFIG_APP_MQTT_RECONNECT_MIN_MS;
		}

		delay = next_backoff(&backoff_ms);
		LOG_WRN("Session ended (%d), reconnecting in %u ms", ret, delay);
		sleep_fed(delay);
	}

	return 0;
}

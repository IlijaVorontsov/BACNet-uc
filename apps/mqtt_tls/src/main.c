/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * MQTT over Ethernet + TLS for STM32 (Zephyr 3.7 LTS).
 *
 * main() waits for the network, runs MQTT sessions back to back and applies
 * an exponential back-off with jitter between failed attempts, so a fleet of
 * devices does not reconnect in lock-step after a broker outage.
 */

#include <errno.h>
#include <string.h>

#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/random/random.h>

#include "app.h"

LOG_MODULE_REGISTER(app, CONFIG_APP_LOG_LEVEL);

BUILD_ASSERT(CONFIG_APP_MQTT_RECONNECT_MIN_MS > 0 &&
	     CONFIG_APP_MQTT_RECONNECT_MIN_MS <= CONFIG_APP_MQTT_RECONNECT_MAX_MS,
	     "invalid reconnect back-off range");

#if DT_NODE_HAS_STATUS(DT_ALIAS(led0), okay)
#define HAVE_LED 1
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
#else
#define HAVE_LED 0
#endif

static bool led_ready;
static bool led_state;

static void led_init(void)
{
#if HAVE_LED
	if (!gpio_is_ready_dt(&led)) {
		LOG_WRN("LED not ready");
		return;
	}
	if (gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE) == 0) {
		led_ready = true;
	}
#endif
}

static int led_set(bool on)
{
#if HAVE_LED
	if (led_ready) {
		int ret = gpio_pin_set_dt(&led, on ? 1 : 0);

		if (ret == 0) {
			led_state = on;
		}
		return ret;
	}
#endif
	ARG_UNUSED(on);
	return -ENODEV;
}

void app_handle_command(const char *cmd, char *reply, size_t reply_len)
{
	int ret = 0;

	if (strcmp(cmd, "ping") == 0) {
		snprintk(reply, reply_len, "{\"pong\":%u}", k_uptime_seconds());
		return;
	}

	if (strcmp(cmd, "led on") == 0) {
		ret = led_set(true);
	} else if (strcmp(cmd, "led off") == 0) {
		ret = led_set(false);
	} else if (strcmp(cmd, "led toggle") == 0) {
		ret = led_set(!led_state);
	} else {
		snprintk(reply, reply_len, "{\"error\":\"unknown command\"}");
		return;
	}

	if (ret < 0) {
		snprintk(reply, reply_len, "{\"error\":\"led unavailable\",\"code\":%d}", ret);
	} else {
		snprintk(reply, reply_len, "{\"led\":%s}", led_state ? "true" : "false");
	}
}

/* Exponential back-off; each delay is randomised to 75..125 % of the base. */
static uint32_t next_backoff(uint32_t *backoff_ms)
{
	uint32_t base = *backoff_ms;
	uint32_t jitter = base / 2U;
	uint32_t delay = base - base / 4U + (jitter ? sys_rand32_get() % jitter : 0U);

	*backoff_ms = MIN(base * 2U, (uint32_t)CONFIG_APP_MQTT_RECONNECT_MAX_MS);

	return delay;
}

int main(void)
{
	uint32_t backoff_ms = CONFIG_APP_MQTT_RECONNECT_MIN_MS;
	int ret;

	LOG_INF("MQTT over Ethernet + TLS on %s", CONFIG_BOARD);

	led_init();

	ret = app_mqtt_init();
	if (ret < 0) {
		return ret;
	}

#if defined(CONFIG_APP_MQTT_TLS)
	ret = app_tls_creds_register();
	if (ret < 0) {
		return ret;
	}
#endif

	app_net_init();

	while (true) {
		bool healthy;
		uint32_t delay;

		if (!app_net_is_up()) {
			LOG_INF("Waiting for network...");
			(void)app_net_wait_up(K_FOREVER);
		}

		ret = app_mqtt_run_session(&healthy);
		if (healthy) {
			backoff_ms = CONFIG_APP_MQTT_RECONNECT_MIN_MS;
		}

		delay = next_backoff(&backoff_ms);
		LOG_WRN("Session ended (%d), reconnecting in %u ms", ret, delay);
		k_sleep(K_MSEC(delay));
	}

	return 0;
}

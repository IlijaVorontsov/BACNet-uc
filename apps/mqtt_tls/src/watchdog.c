/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Last line of defence for an unattended device: a task watchdog channel,
 * backed by the hardware watchdog (STM32 IWDG, NXP WWDT), reboots the MCU if the
 * main loop stops making progress. Every blocking call in the application
 * is bounded well below the channel timeout, so a reboot only happens on a
 * genuine wedge.
 */

#include <zephyr/device.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/task_wdt/task_wdt.h>

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

static int channel = -1;

int app_wdt_init(void)
{
	const struct device *hw_wdt = DEVICE_DT_GET_OR_NULL(DT_ALIAS(watchdog0));
	int ret;

	if (hw_wdt != NULL && !device_is_ready(hw_wdt)) {
		LOG_WRN("Hardware watchdog not ready, using the software task watchdog only");
		hw_wdt = NULL;
	}

	ret = task_wdt_init(hw_wdt);
	if (ret < 0) {
		LOG_ERR("task_wdt_init: %d", ret);
		return ret;
	}

	/* A NULL callback makes the task watchdog reset the system. */
	channel = task_wdt_add(CONFIG_APP_WATCHDOG_TIMEOUT_SEC * MSEC_PER_SEC, NULL, NULL);
	if (channel < 0) {
		LOG_ERR("task_wdt_add: %d", channel);
		return channel;
	}

	LOG_INF("Watchdog armed (%d s%s)", CONFIG_APP_WATCHDOG_TIMEOUT_SEC,
		hw_wdt != NULL ? ", hardware fallback" : "");

	return 0;
}

void app_wdt_feed(void)
{
	if (channel >= 0) {
		(void)task_wdt_feed(channel);
	}
}

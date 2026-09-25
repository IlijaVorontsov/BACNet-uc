/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * HIL stimulus firmware (apps/hil_stimulus), Zephyr v4.4.2.
 * Everything happens in the shell ("stim" group). main() prints the banner,
 * feeds the IWDG and blinks LD1. If a command overruns its own deadline, the
 * watchdog is starved and the stimulus resets into the hardware fail-safe
 * state (all DUT-facing pins Hi-Z, NRST released, DUT powered).
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/shell/shell.h>
#include <zephyr/shell/shell_uart.h>

#include "stim.h"

#define WDT_NODE DT_ALIAS(watchdog0)
#define WDT_TIMEOUT_MS 4000

static const struct gpio_dt_spec led_hb = GPIO_DT_SPEC_GET_OR(DT_ALIAS(led0), gpios, {0});

int main(void)
{
	const struct shell *sh = shell_backend_uart_get_ptr();
	uint32_t cause = 0;
	int rc = stim_init();
	int wdt_ch = -1;
	const struct device *wdt = DEVICE_DT_GET_OR_NULL(WDT_NODE);

	(void)hwinfo_get_reset_cause(&cause);
	(void)hwinfo_clear_reset_cause();

	if (wdt != NULL && device_is_ready(wdt)) {
		struct wdt_timeout_cfg cfg = {
			.window = {.min = 0, .max = WDT_TIMEOUT_MS},
			.flags = WDT_FLAG_RESET_SOC,
		};

		wdt_ch = wdt_install_timeout(wdt, &cfg);
		if (wdt_ch >= 0 && wdt_setup(wdt, WDT_OPT_PAUSE_HALTED_BY_DBG) != 0) {
			wdt_ch = -1;
		}
	}

	if (led_hb.port != NULL) {
		(void)gpio_pin_configure_dt(&led_hb, GPIO_OUTPUT_INACTIVE);
	}

	while (!shell_ready(sh)) {
		k_msleep(10);
	}
	/* Host waits for this line (or the prompt) after every stimulus reset. */
	shell_print(sh, "STIM READY proto=%d fw=%s board=%s rc=0x%08x init=%d", STIM_PROTO,
		    STIM_FW_VERSION, CONFIG_BOARD_TARGET, cause, rc);

	for (;;) {
		if (wdt_ch >= 0 && !stim_cmd_overrun()) {
			(void)wdt_feed(wdt, wdt_ch);
		}
		if (led_hb.port != NULL) {
			(void)gpio_pin_toggle_dt(&led_hb);
		}
		k_msleep(250);
	}
	return 0;
}

/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Remote management glue: MCUboot image confirmation and the "mgmt"/"boot"
 * members of the retained info message.
 *
 * The SMP server itself (MCUmgr over UDP) is started by Zephyr from Kconfig;
 * the handlers are the OS group (echo, reset, info), the settings group
 * (runtime configuration, see config.c) and, with MCUboot, the image group.
 *
 * After an update, MCUboot runs the new image in test mode. The image is
 * confirmed only once it reached "online" (TLS up, SUBACK accepted), i.e.
 * once it has proven that it can still be managed. If that does not happen
 * within APP_MCUBOOT_CONFIRM_TIMEOUT_SEC, the device reboots and MCUboot
 * reverts to the previous image.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/reboot.h>

#if defined(CONFIG_BOOTLOADER_MCUBOOT)
#include <zephyr/dfu/mcuboot.h>
#endif

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

#if defined(CONFIG_BOOTLOADER_MCUBOOT)
static bool confirmed;
#endif

void app_mgmt_init(void)
{
#if defined(CONFIG_BOOTLOADER_MCUBOOT)
	confirmed = boot_is_img_confirmed();
	if (!confirmed) {
		LOG_WRN("Running a test image: it is confirmed once online, or reverted "
			"after %d s", CONFIG_APP_MCUBOOT_CONFIRM_TIMEOUT_SEC);
	}
#endif
}

void app_mgmt_online(void)
{
#if defined(CONFIG_BOOTLOADER_MCUBOOT)
	if (!confirmed) {
		int ret = boot_write_img_confirmed();

		if (ret == 0) {
			confirmed = true;
			LOG_INF("Image confirmed");
		} else {
			LOG_ERR("Image confirmation failed: %d", ret);
		}
	}
#endif
}

void app_mgmt_poll(void)
{
#if defined(CONFIG_BOOTLOADER_MCUBOOT)
	if (!confirmed &&
	    k_uptime_get() >= (int64_t)CONFIG_APP_MCUBOOT_CONFIRM_TIMEOUT_SEC * MSEC_PER_SEC) {
		LOG_ERR("Test image never came online: rebooting so MCUboot reverts it");
		/* Give the log a moment to reach the console. */
		k_sleep(K_MSEC(500));
		sys_reboot(SYS_REBOOT_COLD);
	}
#endif
}

int app_mgmt_info_json(char *buf, size_t len)
{
#if defined(CONFIG_MCUMGR_TRANSPORT_UDP)
	return snprintk(buf, len, "\"mgmt\":{\"smp\":\"udp:%d\"},\"boot\":\"%s\"",
			CONFIG_MCUMGR_TRANSPORT_UDP_PORT,
			IS_ENABLED(CONFIG_BOOTLOADER_MCUBOOT) ? "mcuboot" : "none");
#else
	return snprintk(buf, len, "\"mgmt\":{},\"boot\":\"%s\"",
			IS_ENABLED(CONFIG_BOOTLOADER_MCUBOOT) ? "mcuboot" : "none");
#endif
}

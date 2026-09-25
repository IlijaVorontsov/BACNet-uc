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

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/reboot.h>

#if defined(CONFIG_MCUMGR_GRP_SETTINGS_ACCESS_HOOK)
#include <zephyr/mgmt/mcumgr/mgmt/callbacks.h>
#include <zephyr/mgmt/mcumgr/mgmt/mgmt_defines.h>
#include <zephyr/mgmt/mcumgr/grp/settings_mgmt/settings_mgmt_callbacks.h>
#endif

#if defined(CONFIG_BOOTLOADER_MCUBOOT)
#include <zephyr/dfu/mcuboot.h>
#endif

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

#if defined(CONFIG_BOOTLOADER_MCUBOOT)
static bool confirmed;
static struct k_work_delayable revert_work;

static void revert_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	if (boot_is_img_confirmed()) {
		/* Confirmed some other way, e.g. SMP "image confirm". */
		return;
	}

	LOG_ERR("Test image never came online: rebooting so MCUboot reverts it");
	/* Give the log a moment to reach the console. */
	k_sleep(K_MSEC(500));
	sys_reboot(SYS_REBOOT_COLD);
}
#endif

#if defined(CONFIG_MCUMGR_GRP_SETTINGS_ACCESS_HOOK)
static bool settings_name_allowed(const char *name, bool write)
{
	static const char prefix[] = "mqtt/";

	if (name == NULL || strncmp(name, prefix, sizeof(prefix) - 1) != 0) {
		return false;
	}
	if (write && (strcmp(name, "mqtt/lkg") == 0 || strcmp(name, "mqtt/trial") == 0)) {
		return false;
	}

	return true;
}

static enum mgmt_cb_return settings_access(uint32_t event, enum mgmt_cb_return prev_status,
					   int32_t *rc, uint16_t *group, bool *abort_more,
					   void *data, size_t data_size)
{
	const struct settings_mgmt_access *acc = data;
	bool allowed;

	ARG_UNUSED(prev_status);
	ARG_UNUSED(group);
	ARG_UNUSED(abort_more);

	if (event != MGMT_EVT_OP_SETTINGS_MGMT_ACCESS || data_size != sizeof(*acc)) {
		return MGMT_CB_OK;
	}

	switch (acc->access) {
	case SETTINGS_ACCESS_READ:
		allowed = settings_name_allowed(acc->name, false);
		break;
	case SETTINGS_ACCESS_WRITE:
		allowed = settings_name_allowed(acc->name, true);
		break;
	case SETTINGS_ACCESS_SAVE:
		/* Saving everything (no name) or the mqtt/ subtree. */
		allowed = acc->name == NULL || acc->name[0] == '\0' ||
			  strcmp(acc->name, "mqtt") == 0 ||
			  settings_name_allowed(acc->name, false);
		break;
	case SETTINGS_ACCESS_DELETE:
		allowed = false;
		break;
	default:
		allowed = true;
		break;
	}

	if (!allowed) {
		LOG_WRN("SMP settings access %d to %s refused", (int)acc->access,
			acc->name != NULL ? acc->name : "(all)");
		*rc = MGMT_ERR_EACCESSDENIED;
		return MGMT_CB_ERROR_RC;
	}

	return MGMT_CB_OK;
}

static struct mgmt_callback settings_access_cb = {
	.callback = settings_access,
	.event_id = MGMT_EVT_OP_SETTINGS_MGMT_ACCESS,
};
#endif

void app_mgmt_init(void)
{
#if defined(CONFIG_MCUMGR_GRP_SETTINGS_ACCESS_HOOK)
	mgmt_callback_register(&settings_access_cb);
#endif
#if defined(CONFIG_BOOTLOADER_MCUBOOT)
	confirmed = boot_is_img_confirmed();
	if (!confirmed) {
		/* A work item, not a check in the main loop: it fires even if
		 * the MQTT thread is stuck in a blocking call.
		 */
		k_work_init_delayable(&revert_work, revert_handler);
		(void)k_work_schedule(&revert_work,
				      K_SECONDS(CONFIG_APP_MCUBOOT_CONFIRM_TIMEOUT_SEC));
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
			(void)k_work_cancel_delayable(&revert_work);
			LOG_INF("Image confirmed");
		} else {
			LOG_ERR("Image confirmation failed: %d", ret);
		}
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

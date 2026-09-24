/*
 * SPDX-License-Identifier: Apache-2.0
 */

#include <errno.h>
#include <string.h>

#include <zephyr/drivers/hwinfo.h>
#include <zephyr/logging/log.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/util.h>

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

int app_client_id_get(char *buf, size_t len)
{
	uint8_t id[16];
	ssize_t id_len;
	size_t prefix_len;

	if (sizeof(CONFIG_APP_MQTT_CLIENT_ID) > 1) {
		if (strlen(CONFIG_APP_MQTT_CLIENT_ID) >= len) {
			return -ENOSPC;
		}
		strcpy(buf, CONFIG_APP_MQTT_CLIENT_ID);
		return 0;
	}

	id_len = hwinfo_get_device_id(id, sizeof(id));
	if (id_len <= 0) {
		/* No unique ID on this target: a random ID is unique, but it
		 * changes on every boot, and so do the device's topics.
		 */
		LOG_WRN("No hardware device ID (%d), using a random client ID",
			(int)id_len);
		id_len = 8;
		sys_rand_get(id, id_len);
	}

	prefix_len = strlen(CONFIG_APP_MQTT_CLIENT_ID_PREFIX);
	/* "<prefix>-" + 2 hex digits per byte + NUL */
	if (prefix_len + 1 + 2 * (size_t)id_len + 1 > len) {
		return -ENOSPC;
	}

	memcpy(buf, CONFIG_APP_MQTT_CLIENT_ID_PREFIX, prefix_len);
	buf[prefix_len] = '-';
	bin2hex(id, id_len, &buf[prefix_len + 1], len - prefix_len - 1);

	return 0;
}

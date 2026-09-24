/*
 * SPDX-License-Identifier: Apache-2.0
 */

#include <errno.h>
#include <string.h>

#include <zephyr/drivers/hwinfo.h>
#include <zephyr/logging/log.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/util.h>

#if defined(CONFIG_PSA_WANT_ALG_SHA_256)
#include <psa/crypto.h>
#endif

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

/* 96 bits encode to 20 base32 characters, which leaves room for a short
 * prefix within the 23 characters every MQTT 3.1.1 broker must accept.
 */
#define CLIENT_ID_BYTES 12

/* Lower-case base32hex (RFC 4648, section 7): alphanumeric only, so the
 * result stays inside the character set every MQTT 3.1.1 broker must accept.
 */
static const char base32_alphabet[] = "0123456789abcdefghijklmnopqrstuv";

/* Encode @p len bytes losslessly, 5 bits per character (MSB first).
 * Returns the number of characters written (excluding the NUL), or
 * -ENOSPC.
 */
static int base32_encode(const uint8_t *in, size_t len, char *out, size_t out_len)
{
	size_t chars = DIV_ROUND_UP(len * 8U, 5U);
	uint32_t acc = 0U;
	unsigned int bits = 0U;
	size_t o = 0U;

	if (chars + 1U > out_len) {
		return -ENOSPC;
	}

	for (size_t i = 0U; i < len; i++) {
		acc = (acc << 8) | in[i];
		bits += 8U;
		while (bits >= 5U) {
			bits -= 5U;
			out[o++] = base32_alphabet[(acc >> bits) & 0x1fU];
		}
	}
	if (bits > 0U) {
		out[o++] = base32_alphabet[(acc << (5U - bits)) & 0x1fU];
	}
	out[o] = '\0';

	return (int)o;
}

int app_client_id_get(char *buf, size_t len)
{
	uint8_t id[16];
	ssize_t id_len;
	size_t prefix_len;
	int ret;

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

	if (id_len > CLIENT_ID_BYTES) {
		/* Longer UIDs (128 bits on NXP MCX) are condensed to 96 bits:
		 * with SHA-256 where available, else by XOR-folding.
		 */
		uint8_t folded[CLIENT_ID_BYTES] = { 0 };
#if defined(CONFIG_PSA_WANT_ALG_SHA_256)
		uint8_t digest[32];
		size_t digest_len;

		if (psa_crypto_init() == PSA_SUCCESS &&
		    psa_hash_compute(PSA_ALG_SHA_256, id, id_len, digest, sizeof(digest),
				     &digest_len) == PSA_SUCCESS) {
			memcpy(folded, digest, sizeof(folded));
		} else
#endif
		{
			for (ssize_t i = 0; i < id_len; i++) {
				folded[i % CLIENT_ID_BYTES] ^= id[i];
			}
		}
		memcpy(id, folded, sizeof(folded));
		id_len = CLIENT_ID_BYTES;
	}

	prefix_len = strlen(CONFIG_APP_MQTT_CLIENT_ID_PREFIX);
	if (prefix_len >= len) {
		return -ENOSPC;
	}
	memcpy(buf, CONFIG_APP_MQTT_CLIENT_ID_PREFIX, prefix_len);

	ret = base32_encode(id, id_len, &buf[prefix_len], len - prefix_len);

	return (ret < 0) ? ret : 0;
}

void app_hwid_get(char *buf, size_t len)
{
	uint8_t id[16];
	ssize_t id_len = hwinfo_get_device_id(id, sizeof(id));

	if (id_len <= 0 || 2U * (size_t)id_len + 1U > len) {
		strncpy(buf, "unknown", len - 1);
		buf[len - 1] = '\0';
		return;
	}

	bin2hex(id, id_len, buf, len);
}

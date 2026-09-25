/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Runtime configuration (settings subtree "mqtt/").
 *
 * Kconfig provides the defaults. A key overrides its default only after it
 * has been written explicitly, through the MQTT config_set command or an SMP
 * settings write (+ save). config_reset, or writing "mqtt/factory_reset" over
 * SMP, deletes every stored key and returns to the Kconfig values.
 *
 * All values are stored as text so that SMP clients can write them directly.
 *
 * Last known good: when a key that affects the connection changes, the new
 * configuration is on trial. It becomes the last known good (LKG) once a
 * session reaches "online". If it fails APP_CONFIG_FALLBACK_ATTEMPTS
 * connection attempts in a row, the stored keys are rolled back to the LKG
 * (or to the Kconfig defaults if there is none), so a typo cannot strand a
 * remote device. The trial counter is persisted, so reboots do not reset it.
 */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/util.h>

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

#define SUBTREE "mqtt"
#define SECRET_MASK "***"

enum key_type {
	TYPE_STR,
	TYPE_UINT,
	TYPE_LEVEL,
};

#define F_SECRET BIT(0)
#define F_CONNECTION BIT(1) /* takes effect on the next connect, is trialled */
#define F_NONEMPTY BIT(2)
#define F_TOPIC BIT(3) /* no wildcards, no leading '$' */

struct key_desc {
	const char *name;
	enum key_type type;
	uint8_t flags;
	size_t offset;
	size_t size;     /* TYPE_STR: buffer size */
	uint32_t min;    /* TYPE_UINT */
	uint32_t max;
};

#define STR_KEY(n, field, fl)                                                  \
	{ .name = n, .type = TYPE_STR, .flags = (fl),                          \
	  .offset = offsetof(struct app_config, field),                        \
	  .size = sizeof(((struct app_config *)0)->field) }
#define UINT_KEY(n, field, fl, lo, hi)                                         \
	{ .name = n, .type = TYPE_UINT, .flags = (fl),                         \
	  .offset = offsetof(struct app_config, field),                        \
	  .size = sizeof(((struct app_config *)0)->field), .min = lo, .max = hi }

static const struct key_desc keys[] = {
	STR_KEY("broker_host", broker_host, F_CONNECTION | F_NONEMPTY),
	UINT_KEY("broker_port", broker_port, F_CONNECTION, 1, 65535),
	STR_KEY("tls_hostname", tls_hostname, F_CONNECTION),
	STR_KEY("username", username, F_CONNECTION),
	STR_KEY("password", password, F_CONNECTION | F_SECRET),
	STR_KEY("topic_root", topic_root, F_CONNECTION | F_NONEMPTY | F_TOPIC),
	UINT_KEY("publish_interval", publish_interval, 0, 1, 86400),
	UINT_KEY("keepalive", keepalive, F_CONNECTION, 5, 65535),
	{ .name = "log_level", .type = TYPE_LEVEL,
	  .offset = offsetof(struct app_config, log_level),
	  .size = sizeof(((struct app_config *)0)->log_level) },
};

BUILD_ASSERT(ARRAY_SIZE(keys) <= 32, "explicit-key mask is 32 bits");

/* Persisted last known good: which keys were explicit, and their values. */
struct lkg_blob {
	uint32_t mask;
	struct app_config cfg;
};

static K_MUTEX_DEFINE(lock);
static struct app_config defaults;
static struct app_config current;  /* defaults overlaid with stored keys */
static uint32_t explicit_mask;      /* keys that are stored */
static struct lkg_blob lkg;
static bool have_lkg;
/* > 0: connection config on trial. The new settings get trial_left - 1
 * attempts; the attempt that brings the counter to 0 falls back instead.
 */
static int trial_left;
static bool loading;

static const char *const level_names[] = { "off", "err", "wrn", "inf", "dbg" };

static void *field(struct app_config *cfg, const struct key_desc *k)
{
	return (uint8_t *)cfg + k->offset;
}

static uint32_t get_uint(const struct app_config *cfg, const struct key_desc *k)
{
	const void *p = (const uint8_t *)cfg + k->offset;

	switch (k->size) {
	case sizeof(uint8_t):
		return *(const uint8_t *)p;
	case sizeof(uint16_t):
		return *(const uint16_t *)p;
	default:
		return *(const uint32_t *)p;
	}
}

static void put_uint(struct app_config *cfg, const struct key_desc *k, uint32_t v)
{
	void *p = field(cfg, k);

	switch (k->size) {
	case sizeof(uint8_t):
		*(uint8_t *)p = (uint8_t)v;
		break;
	case sizeof(uint16_t):
		*(uint16_t *)p = (uint16_t)v;
		break;
	default:
		*(uint32_t *)p = v;
		break;
	}
}

static const struct key_desc *find_key(const char *name, size_t *idx)
{
	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		if (strcmp(keys[i].name, name) == 0) {
			if (idx != NULL) {
				*idx = i;
			}
			return &keys[i];
		}
	}

	return NULL;
}

/* Parse and validate @p text into @p cfg. Returns 0 or -EINVAL/-ENAMETOOLONG. */
static int parse_value(const struct key_desc *k, const char *text, struct app_config *cfg)
{
	size_t len = strlen(text);

	switch (k->type) {
	case TYPE_STR:
		if (len >= k->size) {
			return -ENAMETOOLONG;
		}
		if ((k->flags & F_NONEMPTY) && len == 0U) {
			return -EINVAL;
		}
		if ((k->flags & F_TOPIC) &&
		    (strpbrk(text, "+#") != NULL || text[0] == '$')) {
			return -EINVAL;
		}
		if (strpbrk(text, "\"\\") != NULL) {
			/* Keep values safe to echo into JSON unescaped. */
			return -EINVAL;
		}
		for (size_t i = 0; i < len; i++) {
			if ((unsigned char)text[i] < 0x20) {
				return -EINVAL;
			}
		}
		memcpy(field(cfg, k), text, len + 1);
		return 0;

	case TYPE_UINT: {
		char *end;
		unsigned long v;

		if (len == 0U) {
			return -EINVAL;
		}
		v = strtoul(text, &end, 10);
		if (*end != '\0' || v < k->min || v > k->max) {
			return -EINVAL;
		}
		put_uint(cfg, k, (uint32_t)v);
		return 0;
	}

	case TYPE_LEVEL:
		for (size_t i = 0; i < ARRAY_SIZE(level_names); i++) {
			if (strcmp(text, level_names[i]) == 0) {
				put_uint(cfg, k, (uint32_t)i);
				return 0;
			}
		}
		return -EINVAL;
	}

	return -EINVAL;
}

static void format_value(const struct key_desc *k, const struct app_config *cfg,
			 bool reveal, char *buf, size_t len)
{
	if ((k->flags & F_SECRET) && !reveal) {
		/* Write-only: say whether one is set, never what it is. */
		const char *v = (const char *)((const uint8_t *)cfg + k->offset);

		snprintk(buf, len, "%s", v[0] != '\0' ? SECRET_MASK : "");
		return;
	}

	switch (k->type) {
	case TYPE_STR:
		snprintk(buf, len, "%s", (const char *)((const uint8_t *)cfg + k->offset));
		break;
	case TYPE_UINT:
		snprintk(buf, len, "%u", get_uint(cfg, k));
		break;
	case TYPE_LEVEL: {
		uint32_t lvl = get_uint(cfg, k);

		snprintk(buf, len, "%s",
			 lvl < ARRAY_SIZE(level_names) ? level_names[lvl] : "wrn");
		break;
	}
	}
}

static bool connection_differs(const struct app_config *a, const struct app_config *b)
{
	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		const struct key_desc *k = &keys[i];

		if ((k->flags & F_CONNECTION) &&
		    memcmp((const uint8_t *)a + k->offset, (const uint8_t *)b + k->offset,
			   k->size) != 0) {
			return true;
		}
	}

	return false;
}

static void persist_trial(void)
{
	char text[12];

	if (trial_left > 0) {
		snprintk(text, sizeof(text), "%d", trial_left);
		(void)settings_save_one(SUBTREE "/trial", text, strlen(text));
	} else {
		(void)settings_delete(SUBTREE "/trial");
	}
}

/* Start a trial if the connection settings now differ from what last
 * worked (the LKG, or the defaults before the first success).
 */
static void update_trial(void)
{
	const struct app_config *reference = have_lkg ? &lkg.cfg : &defaults;

	if (connection_differs(&current, reference)) {
		trial_left = CONFIG_APP_CONFIG_FALLBACK_ATTEMPTS + 1;
	} else {
		trial_left = 0;
	}
	if (!loading) {
		persist_trial();
	}
}

static void init_defaults(void)
{
	memset(&defaults, 0, sizeof(defaults));
	strncpy(defaults.broker_host, CONFIG_APP_MQTT_BROKER_HOSTNAME,
		sizeof(defaults.broker_host) - 1);
	defaults.broker_port = CONFIG_APP_MQTT_BROKER_PORT;
#if defined(CONFIG_APP_MQTT_TLS)
	strncpy(defaults.tls_hostname, CONFIG_APP_MQTT_TLS_HOSTNAME,
		sizeof(defaults.tls_hostname) - 1);
#endif
	strncpy(defaults.username, CONFIG_APP_MQTT_USERNAME, sizeof(defaults.username) - 1);
	strncpy(defaults.password, CONFIG_APP_MQTT_PASSWORD, sizeof(defaults.password) - 1);
	strncpy(defaults.topic_root, CONFIG_APP_MQTT_TOPIC_ROOT,
		sizeof(defaults.topic_root) - 1);
	defaults.publish_interval = CONFIG_APP_MQTT_PUBLISH_INTERVAL_SEC;
	defaults.keepalive = CONFIG_MQTT_KEEPALIVE;
	defaults.log_level = CONFIG_APP_LOG_MQTT_LEVEL;
}

BUILD_ASSERT(sizeof(CONFIG_APP_MQTT_BROKER_HOSTNAME) <= APP_CFG_STR_LEN &&
	     sizeof(CONFIG_APP_MQTT_TOPIC_ROOT) <= APP_CFG_STR_LEN &&
	     sizeof(CONFIG_APP_MQTT_USERNAME) <= APP_CFG_STR_LEN &&
	     sizeof(CONFIG_APP_MQTT_PASSWORD) <= APP_CFG_STR_LEN,
	     "a Kconfig default does not fit APP_CFG_STR_LEN");

/* --- settings handler -------------------------------------------------- */

static struct k_work factory_reset_work;

static int h_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	const struct key_desc *k;
	size_t idx;
	char text[APP_CFG_STR_LEN];
	int ret = 0;
	ssize_t n;

	if (settings_name_steq(name, "lkg", NULL)) {
		if (len != sizeof(lkg)) {
			return -EINVAL;
		}
		k_mutex_lock(&lock, K_FOREVER);
		n = read_cb(cb_arg, &lkg, sizeof(lkg));
		have_lkg = (n == sizeof(lkg));
		k_mutex_unlock(&lock);
		return have_lkg ? 0 : -EIO;
	}

	if (len >= sizeof(text)) {
		return -ENAMETOOLONG;
	}
	n = read_cb(cb_arg, text, len);
	if (n < 0) {
		return (int)n;
	}
	text[n] = '\0';

	if (settings_name_steq(name, "trial", NULL)) {
		k_mutex_lock(&lock, K_FOREVER);
		trial_left = atoi(text);
		k_mutex_unlock(&lock);
		return 0;
	}

	if (settings_name_steq(name, "factory_reset", NULL)) {
		/* SMP path for a factory reset; runs outside the settings lock. */
		if (!loading) {
			k_work_submit(&factory_reset_work);
		}
		return 0;
	}

	k = find_key(name, &idx);
	if (k == NULL) {
		return -ENOENT;
	}

	k_mutex_lock(&lock, K_FOREVER);
	ret = parse_value(k, text, &current);
	if (ret == 0) {
		explicit_mask |= BIT(idx);
		if (k->flags & F_CONNECTION) {
			if (!loading) {
				update_trial();
			}
		}
	} else {
		LOG_WRN("Ignoring invalid stored setting %s/%s", SUBTREE, name);
	}
	k_mutex_unlock(&lock);

	return ret;
}

static int h_get(const char *name, char *val, int val_len_max)
{
	const struct key_desc *k = find_key(name, NULL);
	char text[APP_CFG_STR_LEN];

	if (k == NULL) {
		return -ENOENT;
	}

	k_mutex_lock(&lock, K_FOREVER);
	format_value(k, &current, false, text, sizeof(text));
	k_mutex_unlock(&lock);

	strncpy(val, text, val_len_max);

	return MIN((int)strlen(text), val_len_max);
}

static int h_export(int (*cb)(const char *name, const void *value, size_t val_len))
{
	char name[48];
	char text[APP_CFG_STR_LEN];

	k_mutex_lock(&lock, K_FOREVER);
	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		if (!(explicit_mask & BIT(i))) {
			continue;
		}
		snprintk(name, sizeof(name), SUBTREE "/%s", keys[i].name);
		format_value(&keys[i], &current, true, text, sizeof(text));
		(void)cb(name, text, strlen(text));
	}
	if (have_lkg) {
		(void)cb(SUBTREE "/lkg", &lkg, sizeof(lkg));
	}
	k_mutex_unlock(&lock);

	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(mqtt_cfg, SUBTREE, h_get, h_set, NULL, h_export);

static void factory_reset_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	(void)app_config_reset();
}

/* --- public API ---------------------------------------------------------- */

int app_config_init(void)
{
	int ret;

	init_defaults();
	current = defaults;
	k_work_init(&factory_reset_work, factory_reset_handler);

	ret = settings_subsys_init();
	if (ret < 0) {
		LOG_ERR("Settings storage unavailable (%d): using Kconfig defaults only", ret);
		return 0;
	}

	loading = true;
	ret = settings_load_subtree(SUBTREE);
	loading = false;
	if (ret < 0) {
		LOG_WRN("Loading settings failed: %d", ret);
	}

	k_mutex_lock(&lock, K_FOREVER);
	if (explicit_mask != 0U) {
		LOG_INF("Stored configuration overrides %u setting(s)",
			(unsigned int)POPCOUNT(explicit_mask));
	}
	if (trial_left > 0) {
		LOG_WRN("Connection settings on trial (%d attempt(s) left before fallback)",
			trial_left - 1);
	}
	k_mutex_unlock(&lock);

	return 0;
}

void app_config_get(struct app_config *out)
{
	k_mutex_lock(&lock, K_FOREVER);
	*out = current;
	k_mutex_unlock(&lock);
}

int app_config_set(const char *name, const char *value, bool *next_connect)
{
	const struct key_desc *k;
	struct app_config tmp;
	size_t idx;
	char key[48];
	int ret;

	k = find_key(name, &idx);
	if (k == NULL) {
		return -ENOENT;
	}

	k_mutex_lock(&lock, K_FOREVER);
	tmp = current;
	ret = parse_value(k, value, &tmp);
	if (ret == 0) {
		snprintk(key, sizeof(key), SUBTREE "/%s", name);
		ret = settings_save_one(key, value, strlen(value));
		if (ret == 0) {
			current = tmp;
			explicit_mask |= BIT(idx);
			if (k->flags & F_CONNECTION) {
				update_trial();
			}
		}
	}
	k_mutex_unlock(&lock);

	if (next_connect != NULL) {
		*next_connect = (k->flags & F_CONNECTION) != 0U;
	}

	return ret;
}

int app_config_get_value(const char *name, char *buf, size_t len)
{
	const struct key_desc *k = find_key(name, NULL);

	if (k == NULL) {
		return -ENOENT;
	}

	k_mutex_lock(&lock, K_FOREVER);
	format_value(k, &current, false, buf, len);
	k_mutex_unlock(&lock);

	return 0;
}

int app_config_reset(void)
{
	char key[48];

	k_mutex_lock(&lock, K_FOREVER);
	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		snprintk(key, sizeof(key), SUBTREE "/%s", keys[i].name);
		(void)settings_delete(key);
	}
	(void)settings_delete(SUBTREE "/lkg");
	(void)settings_delete(SUBTREE "/trial");
	(void)settings_delete(SUBTREE "/factory_reset");
	current = defaults;
	explicit_mask = 0U;
	have_lkg = false;
	trial_left = 0;
	k_mutex_unlock(&lock);

	LOG_WRN("Configuration reset to the Kconfig defaults");

	return 0;
}

int app_config_json(char *buf, size_t len)
{
	size_t off = 0;
	char text[APP_CFG_STR_LEN];
	int n;

	n = snprintk(buf, len, "{");
	off = MAX(n, 0);

	k_mutex_lock(&lock, K_FOREVER);
	for (size_t i = 0; i < ARRAY_SIZE(keys) && off < len; i++) {
		format_value(&keys[i], &current, false, text, sizeof(text));
		if (keys[i].type == TYPE_UINT) {
			n = snprintk(&buf[off], len - off, "%s\"%s\":%s", i ? "," : "",
				     keys[i].name, text);
		} else {
			n = snprintk(&buf[off], len - off, "%s\"%s\":\"%s\"", i ? "," : "",
				     keys[i].name, text);
		}
		off += MAX(n, 0);
	}
	k_mutex_unlock(&lock);

	if (off + 2 > len) {
		return -ENOMEM;
	}
	buf[off++] = '}';
	buf[off] = '\0';

	return (int)off;
}

void app_config_key_list(char *buf, size_t len)
{
	size_t off = 0;
	int n;

	for (size_t i = 0; i < ARRAY_SIZE(keys) && off < len; i++) {
		n = snprintk(&buf[off], len - off, "%s\"%s\"", i ? "," : "", keys[i].name);
		off += MAX(n, 0);
	}
}

bool app_config_attempt(void)
{
	bool fell_back = false;

	k_mutex_lock(&lock, K_FOREVER);
	if (trial_left > 0) {
		trial_left--;
		if (trial_left == 0) {
			/* Out of attempts: roll back to what last worked. */
			char key[48];
			char text[APP_CFG_STR_LEN];
			struct app_config target = have_lkg ? lkg.cfg : defaults;
			uint32_t mask = have_lkg ? lkg.mask : 0U;

			for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
				snprintk(key, sizeof(key), SUBTREE "/%s", keys[i].name);
				if (mask & BIT(i)) {
					format_value(&keys[i], &target, true, text, sizeof(text));
					(void)settings_save_one(key, text, strlen(text));
				} else {
					(void)settings_delete(key);
				}
			}
			/* Keep the current non-connection settings. */
			for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
				if (!(keys[i].flags & F_CONNECTION)) {
					memcpy(field(&target, &keys[i]), field(&current, &keys[i]),
					       keys[i].size);
					if (explicit_mask & BIT(i)) {
						mask |= BIT(i);
						snprintk(key, sizeof(key), SUBTREE "/%s",
							 keys[i].name);
						format_value(&keys[i], &current, true, text,
							     sizeof(text));
						(void)settings_save_one(key, text, strlen(text));
					}
				}
			}
			current = target;
			explicit_mask = mask;
			fell_back = true;
		}
		persist_trial();
	}
	k_mutex_unlock(&lock);

	if (fell_back) {
		LOG_WRN("New connection settings failed %d times: fell back to the %s",
			CONFIG_APP_CONFIG_FALLBACK_ATTEMPTS,
			have_lkg ? "last known good configuration" : "Kconfig defaults");
	}

	return fell_back;
}

void app_config_online(void)
{
	k_mutex_lock(&lock, K_FOREVER);
	if (!have_lkg || trial_left > 0 || connection_differs(&current, &lkg.cfg) ||
	    lkg.mask != explicit_mask) {
		lkg.mask = explicit_mask;
		lkg.cfg = current;
		have_lkg = true;
		(void)settings_save_one(SUBTREE "/lkg", &lkg, sizeof(lkg));
	}
	if (trial_left > 0) {
		trial_left = 0;
		persist_trial();
		LOG_INF("New connection settings confirmed");
	}
	k_mutex_unlock(&lock);
}

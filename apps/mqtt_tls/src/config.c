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
 * All values are stored as text so that SMP clients can write them directly
 * (an empty string is stored as a single NUL byte, because a zero-length
 * value means "deleted" to the settings subsystem).
 *
 * Last known good: when a key that affects the connection changes, the new
 * configuration is on trial. It becomes the last known good (LKG) once a
 * session that used it reaches "online". If it fails
 * APP_CONFIG_FALLBACK_ATTEMPTS connection attempts, the connection keys roll
 * back to the LKG (or to the Kconfig defaults if none has worked yet), so a
 * typo cannot strand a remote device. The trial counter is persisted, so
 * reboots do not reset it.
 *
 * Locking: `lock` protects the RAM state and is always the innermost lock;
 * no settings (flash) call is ever made while holding it, because the
 * settings subsystem calls our handlers with its own lock held. `store_lock`
 * serialises the application-side writers (MQTT thread, factory-reset work).
 */

#include <ctype.h>
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
#define KEY_LEN 48

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
	size_t size;     /* size of the field */
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
static K_MUTEX_DEFINE(store_lock);
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

static const void *cfield(const struct app_config *cfg, const struct key_desc *k)
{
	return (const uint8_t *)cfg + k->offset;
}

static uint32_t get_uint(const struct app_config *cfg, const struct key_desc *k)
{
	const void *p = cfield(cfg, k);

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
	if (name == NULL) {
		return NULL;
	}

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
		for (size_t i = 0; i < len; i++) {
			unsigned char c = (unsigned char)text[i];

			/* Printable ASCII without '"' and '\': safe to echo
			 * into JSON and to log.
			 */
			if (c < 0x20 || c >= 0x7f || c == '"' || c == '\\') {
				return -EINVAL;
			}
		}
		memcpy(field(cfg, k), text, len + 1);
		return 0;

	case TYPE_UINT: {
		unsigned long v;

		if (len == 0U || len > 10U) {
			return -EINVAL;
		}
		for (size_t i = 0; i < len; i++) {
			if (!isdigit((unsigned char)text[i])) {
				return -EINVAL;
			}
		}
		v = strtoul(text, NULL, 10);
		if (v < k->min || v > k->max) {
			return -EINVAL;
		}
		put_uint(cfg, k, (uint32_t)v);
		return 0;
	}

	case TYPE_LEVEL:
		/* "off" is not offered: the log topic carries WRN+ at least. */
		for (size_t i = 1; i < ARRAY_SIZE(level_names); i++) {
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
		const char *v = cfield(cfg, k);

		snprintk(buf, len, "%s", v[0] != '\0' ? SECRET_MASK : "");
		return;
	}

	switch (k->type) {
	case TYPE_STR:
		snprintk(buf, len, "%s", (const char *)cfield(cfg, k));
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

static bool field_differs(const struct app_config *a, const struct app_config *b,
			  const struct key_desc *k)
{
	return memcmp(cfield(a, k), cfield(b, k), k->size) != 0;
}

static bool connection_differs(const struct app_config *a, const struct app_config *b)
{
	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		if ((keys[i].flags & F_CONNECTION) && field_differs(a, b, &keys[i])) {
			return true;
		}
	}

	return false;
}

/* Under `lock`: (re)start or end the trial, comparing the connection
 * settings with what last worked (the LKG, or the defaults before the first
 * success). RAM only; the caller persists trial_left.
 */
static void update_trial_locked(void)
{
	const struct app_config *reference = have_lkg ? &lkg.cfg : &defaults;

	trial_left = connection_differs(&current, reference)
			     ? CONFIG_APP_CONFIG_FALLBACK_ATTEMPTS + 1
			     : 0;
}

/* --- flash writes (never called with `lock` held) ------------------------ */

static int save_text(const char *name, const char *text)
{
	/* A zero-length value would mean "deleted": store "" as one NUL. */
	return settings_save_one(name, text, text[0] != '\0' ? strlen(text) : 1);
}

static void persist_trial(int value)
{
	char text[12];

	if (value > 0) {
		snprintk(text, sizeof(text), "%d", value);
		(void)settings_save_one(SUBTREE "/trial", text, strlen(text));
	} else {
		(void)settings_delete(SUBTREE "/trial");
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

static void apply_log_level(void)
{
	uint8_t level;

	k_mutex_lock(&lock, K_FOREVER);
	level = current.log_level;
	k_mutex_unlock(&lock);

	app_log_set_level(level);
}

/* Check a stored LKG blob: every field must be a value parse_value accepts. */
static bool lkg_valid(struct lkg_blob *blob)
{
	struct app_config tmp = defaults;
	char text[APP_CFG_STR_LEN];

	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		const struct key_desc *k = &keys[i];

		if (k->type == TYPE_STR) {
			((char *)field(&blob->cfg, k))[k->size - 1] = '\0';
		}
		format_value(k, &blob->cfg, true, text, sizeof(text));
		if (parse_value(k, text, &tmp) != 0) {
			return false;
		}
		/* The text round trip must give back the stored value (an
		 * out-of-range level would otherwise read back as "wrn").
		 */
		if (k->type == TYPE_STR
			    ? strcmp(cfield(&tmp, k), cfield(&blob->cfg, k)) != 0
			    : get_uint(&tmp, k) != get_uint(&blob->cfg, k)) {
			return false;
		}
	}
	blob->mask &= BIT_MASK(ARRAY_SIZE(keys));

	return true;
}

/* --- settings handler -------------------------------------------------- */

static struct k_work factory_reset_work;

static int h_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	const struct key_desc *k;
	struct app_config tmp;
	size_t idx;
	char text[APP_CFG_STR_LEN];
	bool changed;
	int ret;
	ssize_t n;

	if (name == NULL) {
		return -ENOENT;
	}

	/* Internal state: only read back from flash at boot, never written
	 * from outside (the application stores it with settings_save_one,
	 * which does not call this handler).
	 */
	if (settings_name_steq(name, "lkg", NULL)) {
		struct lkg_blob blob;

		if (!loading) {
			return -EACCES;
		}
		if (len != sizeof(blob) || read_cb(cb_arg, &blob, sizeof(blob)) != sizeof(blob) ||
		    !lkg_valid(&blob)) {
			LOG_WRN("Discarding invalid stored last-known-good configuration");
			return 0;
		}
		k_mutex_lock(&lock, K_FOREVER);
		lkg = blob;
		have_lkg = true;
		k_mutex_unlock(&lock);
		return 0;
	}

	if (len >= sizeof(text)) {
		return -ENAMETOOLONG;
	}
	n = read_cb(cb_arg, text, len);
	if (n < 0) {
		return (int)n;
	}
	/* Strip the NUL an empty value is stored as; reject embedded NULs. */
	while (n > 0 && text[n - 1] == '\0') {
		n--;
	}
	text[n] = '\0';
	if (memchr(text, '\0', n) != NULL) {
		return -EINVAL;
	}

	if (settings_name_steq(name, "trial", NULL)) {
		if (!loading) {
			return -EACCES;
		}
		k_mutex_lock(&lock, K_FOREVER);
		trial_left = CLAMP(atoi(text), 0, CONFIG_APP_CONFIG_FALLBACK_ATTEMPTS + 1);
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
	tmp = current;
	ret = parse_value(k, text, &tmp);
	if (ret == 0) {
		changed = field_differs(&tmp, &current, k);
		memcpy(field(&current, k), cfield(&tmp, k), k->size);
		explicit_mask |= BIT(idx);
		/* A runtime (SMP) write of a new connection value starts a
		 * trial; it is persisted by the next SMP save (h_export).
		 * Re-loading identical values leaves a running trial alone.
		 */
		if (!loading && changed && (k->flags & F_CONNECTION)) {
			update_trial_locked();
		}
	} else {
		LOG_WRN("Ignoring invalid setting %s/%s", SUBTREE, name);
	}
	k_mutex_unlock(&lock);

	if (ret == 0 && !loading && k->type == TYPE_LEVEL) {
		apply_log_level();
	}

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
	char texts[ARRAY_SIZE(keys)][APP_CFG_STR_LEN];
	char name[KEY_LEN];
	char trial[12];
	uint32_t mask;
	struct lkg_blob blob;
	bool with_lkg;
	int trial_val;

	/* Snapshot under the lock, write without it (settings_save holds the
	 * settings lock while calling us).
	 */
	k_mutex_lock(&lock, K_FOREVER);
	mask = explicit_mask;
	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		format_value(&keys[i], &current, true, texts[i], sizeof(texts[i]));
	}
	blob = lkg;
	with_lkg = have_lkg;
	trial_val = trial_left;
	k_mutex_unlock(&lock);

	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		if (!(mask & BIT(i))) {
			continue;
		}
		snprintk(name, sizeof(name), SUBTREE "/%s", keys[i].name);
		(void)cb(name, texts[i], texts[i][0] != '\0' ? strlen(texts[i]) : 1);
	}
	if (with_lkg) {
		(void)cb(SUBTREE "/lkg", &blob, sizeof(blob));
	}
	snprintk(trial, sizeof(trial), "%d", MAX(trial_val, 0));
	(void)cb(SUBTREE "/trial", trial, strlen(trial));
	memset(texts, 0, sizeof(texts)); /* the password was in here */

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
		apply_log_level();
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

	apply_log_level();

	return 0;
}

void app_config_get(struct app_config *out)
{
	k_mutex_lock(&lock, K_FOREVER);
	*out = current;
	k_mutex_unlock(&lock);
}

void app_config_snapshot(struct app_config *out, uint32_t *mask)
{
	k_mutex_lock(&lock, K_FOREVER);
	*out = current;
	*mask = explicit_mask;
	k_mutex_unlock(&lock);
}

int app_config_set(const char *name, const char *value, bool *next_connect)
{
	const struct key_desc *k;
	struct app_config tmp;
	size_t idx;
	char key[KEY_LEN];
	bool changed = false;
	int trial = -1;
	int ret;

	k = find_key(name, &idx);
	if (k == NULL) {
		return -ENOENT;
	}
	if (next_connect != NULL) {
		*next_connect = (k->flags & F_CONNECTION) != 0U;
	}

	k_mutex_lock(&store_lock, K_FOREVER);

	k_mutex_lock(&lock, K_FOREVER);
	tmp = current;
	ret = parse_value(k, value, &tmp);
	k_mutex_unlock(&lock);
	if (ret < 0) {
		goto out;
	}

	snprintk(key, sizeof(key), SUBTREE "/%s", name);
	if (save_text(key, value) != 0) {
		ret = -EIO;
		goto out;
	}

	k_mutex_lock(&lock, K_FOREVER);
	changed = field_differs(&tmp, &current, k);
	memcpy(field(&current, k), cfield(&tmp, k), k->size);
	explicit_mask |= BIT(idx);
	if (changed && (k->flags & F_CONNECTION)) {
		update_trial_locked();
		trial = trial_left;
	}
	k_mutex_unlock(&lock);

	if (trial >= 0) {
		persist_trial(trial);
	}
	if (k->type == TYPE_LEVEL) {
		apply_log_level();
	}

out:
	k_mutex_unlock(&store_lock);

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
	char key[KEY_LEN];

	k_mutex_lock(&store_lock, K_FOREVER);

	k_mutex_lock(&lock, K_FOREVER);
	current = defaults;
	explicit_mask = 0U;
	have_lkg = false;
	trial_left = 0;
	k_mutex_unlock(&lock);

	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		snprintk(key, sizeof(key), SUBTREE "/%s", keys[i].name);
		(void)settings_delete(key);
	}
	(void)settings_delete(SUBTREE "/lkg");
	(void)settings_delete(SUBTREE "/trial");
	(void)settings_delete(SUBTREE "/factory_reset");

	k_mutex_unlock(&store_lock);

	apply_log_level();
	LOG_WRN("Configuration reset to the Kconfig defaults");

	return 0;
}

int app_config_json(char *buf, size_t len)
{
	size_t off;
	char text[APP_CFG_STR_LEN];
	int n;

	if (len < 2) {
		return -ENOMEM;
	}
	buf[0] = '{';
	off = 1;

	k_mutex_lock(&lock, K_FOREVER);
	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		format_value(&keys[i], &current, false, text, sizeof(text));
		n = snprintk(&buf[off], len - off,
			     keys[i].type == TYPE_UINT ? "%s\"%s\":%s" : "%s\"%s\":\"%s\"",
			     i ? "," : "", keys[i].name, text);
		if (n < 0 || (size_t)n >= len - off) {
			k_mutex_unlock(&lock);
			return -ENOMEM;
		}
		off += (size_t)n;
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

	buf[0] = '\0';
	for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
		n = snprintk(&buf[off], len - off, "%s\"%s\"", i ? "," : "", keys[i].name);
		if (n < 0 || (size_t)n >= len - off) {
			break;
		}
		off += (size_t)n;
	}
}

bool app_config_attempt(void)
{
	struct {
		char key[KEY_LEN];
		char text[APP_CFG_STR_LEN];
		bool keep;
	} writes[ARRAY_SIZE(keys)];
	size_t n_writes = 0;
	bool fell_back = false;
	bool to_lkg = false;
	int trial = -1;

	k_mutex_lock(&store_lock, K_FOREVER);

	k_mutex_lock(&lock, K_FOREVER);
	if (trial_left > 0) {
		trial_left--;
		trial = trial_left;
		if (trial_left == 0) {
			/* Out of attempts: roll the connection keys back to
			 * what last worked. Other keys stay as they are.
			 */
			to_lkg = have_lkg;
			for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
				const struct key_desc *k = &keys[i];
				bool in_lkg = have_lkg && (lkg.mask & BIT(i));

				if (!(k->flags & F_CONNECTION)) {
					continue;
				}
				memcpy(field(&current, k),
				       in_lkg ? cfield(&lkg.cfg, k) : cfield(&defaults, k), k->size);
				if (in_lkg) {
					explicit_mask |= BIT(i);
				} else {
					explicit_mask &= ~BIT(i);
				}
				snprintk(writes[n_writes].key, KEY_LEN, SUBTREE "/%s", k->name);
				format_value(k, &current, true, writes[n_writes].text, APP_CFG_STR_LEN);
				writes[n_writes].keep = in_lkg;
				n_writes++;
			}
			fell_back = true;
		}
	}
	k_mutex_unlock(&lock);

	for (size_t i = 0; i < n_writes; i++) {
		if (writes[i].keep) {
			(void)save_text(writes[i].key, writes[i].text);
		} else {
			(void)settings_delete(writes[i].key);
		}
	}
	memset(writes, 0, sizeof(writes)); /* may hold the password */
	if (trial >= 0) {
		persist_trial(trial);
	}

	k_mutex_unlock(&store_lock);

	if (fell_back) {
		LOG_WRN("New connection settings failed %d times: fell back to the %s",
			CONFIG_APP_CONFIG_FALLBACK_ATTEMPTS,
			to_lkg ? "last known good configuration" : "Kconfig defaults");
	}

	return fell_back;
}

void app_config_lkg_topic_root(char *buf, size_t len)
{
	k_mutex_lock(&lock, K_FOREVER);
	snprintk(buf, len, "%s", have_lkg ? lkg.cfg.topic_root : "");
	k_mutex_unlock(&lock);
}

void app_config_online(const struct app_config *used, uint32_t used_mask)
{
	struct lkg_blob blob;
	bool save_lkg = false;
	bool confirmed = false;
	int trial = -1;

	k_mutex_lock(&store_lock, K_FOREVER);

	k_mutex_lock(&lock, K_FOREVER);
	/* The configuration this session actually used worked. */
	if (!have_lkg || lkg.mask != used_mask || memcmp(&lkg.cfg, used, sizeof(*used)) != 0) {
		lkg.mask = used_mask;
		lkg.cfg = *used;
		have_lkg = true;
		save_lkg = true;
	}
	blob = lkg;
	if (trial_left > 0) {
		/* If the connection settings changed again while this session
		 * was connecting, the newer ones stay on trial.
		 */
		update_trial_locked();
		trial = trial_left;
		confirmed = (trial_left == 0);
	}
	k_mutex_unlock(&lock);

	if (save_lkg) {
		(void)settings_save_one(SUBTREE "/lkg", &blob, sizeof(blob));
	}
	if (trial >= 0) {
		persist_trial(trial);
	}

	k_mutex_unlock(&store_lock);

	if (confirmed) {
		LOG_INF("New connection settings confirmed");
	}
}

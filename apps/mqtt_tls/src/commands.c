/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Commands received on <root>/<id>/cmd, in either form:
 *
 *   plain text   "ping" | "led on|off|toggle" | "identify [seconds]"
 *   JSON         {"id":"<=16 chars","cmd":"led","arg":"on"}
 *
 * Replies go to <root>/<id>/event as JSON: {"ok":true,...} or
 * {"ok":false,"error":"..."}; a JSON request's "id" is echoed so a client
 * can match replies when several clients send commands.
 */

#include <ctype.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/data/json.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

#define REQUEST_ID_MAX_LEN 16
#define IDENTIFY_DEFAULT_SEC 30
#define IDENTIFY_MAX_SEC 3600
#define IDENTIFY_BLINK_MS 250

#if DT_NODE_HAS_STATUS(DT_ALIAS(led0), okay)
#define HAVE_LED 1
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
#else
#define HAVE_LED 0
#endif

/* The LED is driven from the MQTT thread (commands) and the system work
 * queue (identify blinking).
 */
static K_MUTEX_DEFINE(led_lock);
static bool led_ready;
static bool led_state;        /* commanded state, restored after identify */
static int64_t identify_until; /* 0 when not identifying */
static bool blink_on;
static struct k_work_delayable identify_work;

static void led_write(bool on)
{
#if HAVE_LED
	(void)gpio_pin_set_dt(&led, on ? 1 : 0);
#else
	ARG_UNUSED(on);
#endif
}

static void identify_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	k_mutex_lock(&led_lock, K_FOREVER);
	if (identify_until == 0 || k_uptime_get() >= identify_until) {
		identify_until = 0;
		led_write(led_state);
	} else {
		blink_on = !blink_on;
		led_write(blink_on);
		k_work_reschedule(&identify_work, K_MSEC(IDENTIFY_BLINK_MS));
	}
	k_mutex_unlock(&led_lock);
}

void app_commands_init(void)
{
	k_work_init_delayable(&identify_work, identify_handler);

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
	if (!led_ready) {
		return -ENODEV;
	}

	k_mutex_lock(&led_lock, K_FOREVER);
	identify_until = 0; /* an explicit LED command ends identify */
	led_state = on;
	led_write(on);
	k_mutex_unlock(&led_lock);

	return 0;
}

static int identify(uint32_t seconds)
{
	if (!led_ready) {
		return -ENODEV;
	}

	k_mutex_lock(&led_lock, K_FOREVER);
	identify_until = (seconds == 0U) ? 0 : k_uptime_get() + (int64_t)seconds * MSEC_PER_SEC;
	k_mutex_unlock(&led_lock);

	/* Stopping also runs the handler once, which restores the LED. */
	k_work_reschedule(&identify_work, K_NO_WAIT);

	return 0;
}

struct result {
	bool ok;
	const char *error;
	char detail[48]; /* extra JSON members, without braces */
};

static void execute(const char *cmd, const char *arg, struct result *r)
{
	int ret;

	r->ok = false;
	r->error = NULL;
	r->detail[0] = '\0';

	if (strcmp(cmd, "ping") == 0 && arg == NULL) {
		r->ok = true;
		snprintk(r->detail, sizeof(r->detail), "\"pong\":%u", k_uptime_seconds());
		return;
	}

	if (strcmp(cmd, "led") == 0) {
		bool on;

		if (arg != NULL && strcmp(arg, "on") == 0) {
			on = true;
		} else if (arg != NULL && strcmp(arg, "off") == 0) {
			on = false;
		} else if (arg != NULL && strcmp(arg, "toggle") == 0) {
			on = !led_state;
		} else {
			r->error = "bad argument";
			return;
		}
		ret = led_set(on);
		if (ret < 0) {
			r->error = "led unavailable";
			return;
		}
		r->ok = true;
		snprintk(r->detail, sizeof(r->detail), "\"led\":%s", on ? "true" : "false");
		return;
	}

	if (strcmp(cmd, "identify") == 0) {
		unsigned long seconds = IDENTIFY_DEFAULT_SEC;

		if (arg != NULL) {
			char *end;

			seconds = strtoul(arg, &end, 10);
			if (*arg == '\0' || *end != '\0' || seconds > IDENTIFY_MAX_SEC) {
				r->error = "bad argument";
				return;
			}
		}
		ret = identify((uint32_t)seconds);
		if (ret < 0) {
			r->error = "led unavailable";
			return;
		}
		r->ok = true;
		snprintk(r->detail, sizeof(r->detail), "\"identify\":%lu", seconds);
		return;
	}

	r->error = "unknown command";
}

struct json_request {
	const char *id;
	const char *cmd;
	const char *arg;
};

static const struct json_obj_descr json_request_descr[] = {
	JSON_OBJ_DESCR_PRIM(struct json_request, id, JSON_TOK_STRING),
	JSON_OBJ_DESCR_PRIM(struct json_request, cmd, JSON_TOK_STRING),
	JSON_OBJ_DESCR_PRIM(struct json_request, arg, JSON_TOK_STRING),
};

/* The id is echoed into the reply unescaped, so it is restricted to
 * characters that need no escaping in JSON.
 */
static bool is_valid_request_id(const char *id)
{
	size_t len = strlen(id);

	if (len == 0U || len > REQUEST_ID_MAX_LEN) {
		return false;
	}
	for (size_t i = 0U; i < len; i++) {
		char c = id[i];

		if (!(isalnum((unsigned char)c) || c == '-' || c == '_' || c == '.' ||
		      c == ':')) {
			return false;
		}
	}

	return true;
}

static void format_reply(char *reply, size_t reply_len, const char *id,
			 const struct result *r)
{
	char id_member[REQUEST_ID_MAX_LEN + 8] = "";

	if (id != NULL) {
		snprintk(id_member, sizeof(id_member), "\"id\":\"%s\",", id);
	}

	if (r->ok) {
		snprintk(reply, reply_len, "{%s\"ok\":true%s%s}", id_member,
			 r->detail[0] != '\0' ? "," : "", r->detail);
	} else {
		snprintk(reply, reply_len, "{%s\"ok\":false,\"error\":\"%s\"}", id_member,
			 r->error);
	}
}

void app_handle_command(char *payload, char *reply, size_t reply_len)
{
	struct result r;

	if (payload[0] == '{') {
		struct json_request req = { 0 };
		const char *id = NULL;
		int64_t fields;

		/* Decodes in place; strings point into payload. */
		fields = json_obj_parse(payload, strlen(payload), json_request_descr,
					ARRAY_SIZE(json_request_descr), &req);
		/* Echo the id whenever it is usable, also in error replies. */
		if (fields >= 0 && (fields & BIT(0)) && is_valid_request_id(req.id)) {
			id = req.id;
		}
		r.ok = false;
		if (fields < 0) {
			r.error = "invalid json";
		} else if ((fields & BIT(0)) && id == NULL) {
			r.error = "invalid id";
		} else if (!(fields & BIT(1))) {
			r.error = "missing cmd";
		} else {
			execute(req.cmd, (fields & BIT(2)) ? req.arg : NULL, &r);
		}
		format_reply(reply, reply_len, id, &r);
		return;
	}

	/* Plain text: "<cmd>[ <arg>]" */
	char *arg = strchr(payload, ' ');

	if (arg != NULL) {
		*arg++ = '\0';
	}
	execute(payload, arg, &r);
	format_reply(reply, reply_len, NULL, &r);
}

void app_commands_caps(char *buf, size_t len)
{
	/* Commands the harness can offer; LED commands only with an LED. */
	snprintk(buf, len,
		 "{\"cmds\":[\"ping\"%s],"
		 "\"telemetry\":{\"seq\":\"count\",\"uptime_s\":\"s\",\"sessions\":\"count\"}}",
		 led_ready ? ",\"led\",\"identify\"" : "");
}

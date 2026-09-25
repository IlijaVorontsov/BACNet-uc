/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Logs over MQTT, part 1: a log backend that copies messages at or above the
 * configured level (mqtt/log_level, default WRN) into a RAM ring buffer.
 *
 * The backend only formats and copies under a spinlock. It never touches the
 * network: publishing from the logging context could recurse (the net stack
 * logs too) or block inside the net stack. The MQTT thread drains the ring
 * with app_log_next() and publishes, rate-limited (mqtt_app.c). The ring keeps
 * the messages from before the first connection, so boot problems are
 * visible once the device comes online.
 */

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_backend.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/logging/log_msg.h>
#include <zephyr/logging/log_output.h>
#include <zephyr/sys/cbprintf.h>
#include <zephyr/sys/util.h>

#include "app.h"

static struct app_log_line ring[CONFIG_APP_LOG_MQTT_RING_SIZE];
static uint32_t written; /* total lines ever written; ring index = written % size */
static struct k_spinlock ring_lock;
static atomic_t threshold = ATOMIC_INIT(CONFIG_APP_LOG_MQTT_LEVEL);
/* Messages the logging core dropped (buffer full) before they reached us. */
static atomic_t core_dropped;

#if defined(CONFIG_APP_LOG_MQTT)
struct fmt_ctx {
	char *buf;
	size_t len;
	size_t pos;
};

static int fmt_out(int c, void *arg)
{
	struct fmt_ctx *ctx = arg;

	if (ctx->pos + 1 < ctx->len) {
		ctx->buf[ctx->pos++] = (c == '\n' || c == '\r') ? ' ' : (char)c;
	}

	return c;
}

static void process(const struct log_backend *const backend, union log_msg_generic *msg)
{
	struct app_log_line line;
	struct fmt_ctx ctx = { .buf = line.msg, .len = sizeof(line.msg) };
	uint8_t level = log_msg_get_level(&msg->log);
	int16_t source_id = log_msg_get_source_id(&msg->log);
	const char *src = NULL;
	size_t plen;
	uint8_t *package;
	k_spinlock_key_t key;

	ARG_UNUSED(backend);

	if (level == LOG_LEVEL_NONE || level > (uint8_t)atomic_get(&threshold)) {
		return;
	}

	line.t_ms = (uint32_t)(log_output_timestamp_to_us(log_msg_get_timestamp(&msg->log)) /
			     USEC_PER_MSEC);
	line.level = level;

	if (source_id >= 0) {
		src = log_source_name_get(log_msg_get_domain(&msg->log), source_id);
	}
	strncpy(line.src, src != NULL ? src : "", sizeof(line.src) - 1);
	line.src[sizeof(line.src) - 1] = '\0';

	package = log_msg_get_package(&msg->log, &plen);
	if (plen > 0U) {
		(void)cbpprintf(fmt_out, &ctx, package);
	}
	line.msg[ctx.pos] = '\0';

	key = k_spin_lock(&ring_lock);
	ring[written % ARRAY_SIZE(ring)] = line;
	written++;
	k_spin_unlock(&ring_lock, key);
}

static void panic(const struct log_backend *const backend)
{
	ARG_UNUSED(backend);
}

static void dropped(const struct log_backend *const backend, uint32_t cnt)
{
	ARG_UNUSED(backend);

	(void)atomic_add(&core_dropped, (atomic_val_t)cnt);
}

static const struct log_backend_api api = {
	.process = process,
	.panic = panic,
	.dropped = dropped,
};

LOG_BACKEND_DEFINE(app_log_mqtt, api, true);
#endif /* CONFIG_APP_LOG_MQTT */

void app_log_set_level(uint8_t level)
{
	atomic_set(&threshold, level);
}

bool app_log_next(uint32_t *cursor, struct app_log_line *out, uint32_t *lost)
{
	k_spinlock_key_t key = k_spin_lock(&ring_lock);
	uint32_t oldest = (written > ARRAY_SIZE(ring)) ? written - ARRAY_SIZE(ring) : 0U;
	bool have = false;

	*lost = 0U;
	if (*cursor < written) {
		/* Count core drops against the next line actually delivered. */
		*lost = (uint32_t)atomic_clear(&core_dropped);
	}
	if (*cursor < oldest) {
		*lost += oldest - *cursor;
		*cursor = oldest;
	}
	if (*cursor < written) {
		*out = ring[*cursor % ARRAY_SIZE(ring)];
		(*cursor)++;
		have = true;
	}
	k_spin_unlock(&ring_lock, key);

	return have;
}

uint32_t app_log_count(void)
{
	k_spinlock_key_t key = k_spin_lock(&ring_lock);
	uint32_t n = written;

	k_spin_unlock(&ring_lock, key);

	return n;
}

const char *app_log_level_name(uint8_t level)
{
	static const char *const names[] = { "off", "err", "wrn", "inf", "dbg" };

	return level < ARRAY_SIZE(names) ? names[level] : "?";
}

size_t app_json_escape(char *dst, size_t len, const char *src)
{
	size_t o = 0;

	if (len == 0U) {
		return 0;
	}

	for (; *src != '\0'; src++) {
		unsigned char c = (unsigned char)*src;
		char esc[7];
		size_t n;

		if (c == '"' || c == '\\') {
			esc[0] = '\\';
			esc[1] = (char)c;
			n = 2;
		} else if (c < 0x20) {
			n = snprintk(esc, sizeof(esc), "\\u%04x", c);
		} else {
			esc[0] = (char)c;
			n = 1;
		}
		if (o + n + 1 > len) {
			break;
		}
		memcpy(&dst[o], esc, n);
		o += n;
	}
	dst[o] = '\0';

	return o;
}

int app_log_line_json(const struct app_log_line *line, uint32_t lost, char *buf, size_t len)
{
	char msg[2 * sizeof(line->msg)];
	char src[2 * sizeof(line->src)];
	int n;

	app_json_escape(msg, sizeof(msg), line->msg);
	app_json_escape(src, sizeof(src), line->src);

	if (lost > 0U) {
		n = snprintk(buf, len, "{\"t\":%u,\"lvl\":\"%s\",\"src\":\"%s\",\"msg\":\"%s\","
			     "\"lost\":%u}", line->t_ms, app_log_level_name(line->level), src,
			     msg, lost);
	} else {
		n = snprintk(buf, len, "{\"t\":%u,\"lvl\":\"%s\",\"src\":\"%s\",\"msg\":\"%s\"}",
			     line->t_ms, app_log_level_name(line->level), src, msg);
	}

	return (n < (int)len) ? n : -ENOMEM;
}

/* Format lines [from, written) into buf. Returns the line count, or -ENOMEM
 * if they do not all fit.
 */
static int format_from(uint32_t from, char *buf, size_t len)
{
	struct app_log_line line;
	uint32_t cursor = from;
	uint32_t lost;
	size_t off;
	int count = 0;
	int w;

	w = snprintk(buf, len, "\"logs\":[");
	if (w < 0 || (size_t)w >= len) {
		return -ENOMEM;
	}
	off = (size_t)w;

	while (true) {
		k_spinlock_key_t key = k_spin_lock(&ring_lock);
		uint32_t oldest = (written > ARRAY_SIZE(ring)) ? written - ARRAY_SIZE(ring) : 0U;
		bool have = false;

		/* Unlike app_log_next, do not touch the core drop counter. */
		lost = 0U;
		if (cursor < oldest) {
			cursor = oldest;
		}
		if (cursor < written) {
			line = ring[cursor % ARRAY_SIZE(ring)];
			cursor++;
			have = true;
		}
		k_spin_unlock(&ring_lock, key);
		if (!have) {
			break;
		}

		/* Leave room for "," and the closing "]". */
		if (off + 3 > len) {
			return -ENOMEM;
		}
		w = app_log_line_json(&line, lost, &buf[off + (count ? 1 : 0)],
				      len - off - (count ? 1 : 0) - 1);
		if (w < 0) {
			return -ENOMEM;
		}
		if (count) {
			buf[off] = ',';
		}
		off += (size_t)w + (count ? 1 : 0);
		count++;
	}

	buf[off++] = ']';
	buf[off] = '\0';

	return count;
}

int app_log_last_json(size_t n, char *buf, size_t len)
{
	uint32_t total = app_log_count();
	uint32_t from = (total > n) ? total - (uint32_t)n : 0U;
	int count;

	/* Keep the newest lines: drop the oldest until the rest fit. */
	for (; from <= total; from++) {
		count = format_from(from, buf, len);
		if (count >= 0) {
			return count;
		}
	}

	return format_from(UINT32_MAX, buf, len);
}

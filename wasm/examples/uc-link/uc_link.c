/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * uc-link - copies the Present_Value of source points (local or remote)
 * to destination objects on this node: dst = src * scale + offset.
 *
 * Specification: wasm/examples/uc-link/README.md (parameters "count" and
 * "l0".."l<count-1>", each "<src_device> <src_type> <src_instance>
 * <dst_type> <dst_instance> <cov|poll> <period_ms> <priority> <scale>
 * <offset>"). Details this implementation fixes:
 *   - Fields are separated by blanks; exactly 10 fields. Ranges:
 *     src_device 0..4194302 (4294967295 = UC_DEVICE_LOCAL is accepted as
 *     well), types 0..1023, instances 0..4194302, period_ms 100..3600000
 *     (a cov link may give 0: fallback period 1000 ms), priority 0..16,
 *     scale/offset finite decimal numbers. Anything else is malformed.
 *   - "count" missing or outside 1..8, or no usable link: uc_app_init
 *     fails (the app enters the "failed" state with the reason logged).
 *   - A cov link whose SubscribeCOV request fails (no subscription slot,
 *     bad device) is polled with its period_ms (system.schema.json: "Poll
 *     period, or COV fallback poll period") and the subscription is
 *     retried every 30 s.
 *   - The tick period is the smallest period_ms of all links (cov links
 *     included, as they may fall back to polling), at least 100 ms.
 *   - Destination write errors are logged once per error episode.
 *   - On a regular stop, destinations the app did not create and that
 *     were written with a priority are relinquished at that priority.
 *
 * Permissions: bacnet.local, bacnet.remote.
 */

#include <bacnet_uc.h>
#include <uc_util.h>

UC_APP_DECLARE()

#define LINK_MAX        8
#define COV_LIFETIME_S  300u
#define RESUBSCRIBE_MS  30000u
#define READ_TIMEOUT_MS 2000u
#define PERIOD_MIN_MS   100u
#define PERIOD_MAX_MS   3600000u
#define COV_FALLBACK_MS 1000u
#define FIELD_MAX       24

enum link_mode {
	MODE_COV,
	MODE_POLL
};

struct link {
	bool valid;
	uint32_t src_device;
	uint32_t src_type;
	uint32_t src_instance;
	uint32_t dst_type;
	uint32_t dst_instance;
	enum link_mode mode;
	uint32_t period_ms;
	uint32_t priority;
	double scale;
	double offset;
	/* state */
	bool created;   /* destination created by this app */
	int32_t sub_id; /* cov: >= 0 while subscribed */
	bool have_out;  /* last_out holds the last written value */
	double last_out;
	uint64_t next_poll_ms;
	uint64_t next_sub_ms;
	int32_t read_err;
	int32_t write_err;
	uint32_t writes;
};

static struct {
	struct link links[LINK_MAX];
	uint32_t count;
	uint32_t valid;
} g;

/* ---------------------------------------------------------------------- */
/* Parameter parsing                                                       */
/* ---------------------------------------------------------------------- */

static bool field_u32(const char **p, uint32_t lo, uint32_t hi, uint32_t *out)
{
	char tok[FIELD_MAX];

	return uc_next_token(p, tok, sizeof(tok)) && uc_str_to_u32(tok, out) && (*out >= lo) &&
	       (*out <= hi);
}

static bool field_double(const char **p, double *out)
{
	char tok[FIELD_MAX];

	return uc_next_token(p, tok, sizeof(tok)) && uc_str_to_double(tok, out);
}

/* Parse one link; on failure *why names the offending field. */
static bool link_parse(const char *s, struct link *l, const char **why)
{
	char tok[FIELD_MAX];
	const char *p = s;

	memset(l, 0, sizeof(*l));
	*why = "src_device";
	if (!field_u32(&p, 0, UC_DEVICE_LOCAL, &l->src_device) ||
	    ((l->src_device > UC_INSTANCE_MAX) && (l->src_device != UC_DEVICE_LOCAL))) {
		return false;
	}
	*why = "src_type";
	if (!field_u32(&p, 0, UC_OBJ_TYPE_MAX, &l->src_type)) {
		return false;
	}
	*why = "src_instance";
	if (!field_u32(&p, 0, UC_INSTANCE_MAX, &l->src_instance)) {
		return false;
	}
	*why = "dst_type";
	if (!field_u32(&p, 0, UC_OBJ_TYPE_MAX, &l->dst_type)) {
		return false;
	}
	*why = "dst_instance";
	if (!field_u32(&p, 0, UC_INSTANCE_MAX, &l->dst_instance)) {
		return false;
	}
	*why = "mode";
	if (!uc_next_token(&p, tok, sizeof(tok))) {
		return false;
	}
	if (uc_streq(tok, "cov")) {
		l->mode = MODE_COV;
	} else if (uc_streq(tok, "poll")) {
		l->mode = MODE_POLL;
	} else {
		return false;
	}
	*why = "period_ms";
	if (!field_u32(&p, 0, PERIOD_MAX_MS, &l->period_ms)) {
		return false;
	}
	if (l->period_ms < PERIOD_MIN_MS) {
		if ((l->mode == MODE_POLL) || (l->period_ms != 0u)) {
			return false;
		}
		l->period_ms = COV_FALLBACK_MS;
	}
	*why = "priority";
	if (!field_u32(&p, 0, 16, &l->priority)) {
		return false;
	}
	*why = "scale";
	if (!field_double(&p, &l->scale)) {
		return false;
	}
	*why = "offset";
	if (!field_double(&p, &l->offset)) {
		return false;
	}
	*why = "trailing fields";
	if (*uc_skip_blanks(p) != '\0') {
		return false;
	}
	l->valid = true;
	l->sub_id = -1;
	return true;
}

/* ---------------------------------------------------------------------- */
/* Link operation                                                          */
/* ---------------------------------------------------------------------- */

static void dst_write(uint32_t i, struct link *l, double src)
{
	double out = src * l->scale + l->offset;
	int32_t rc = uc_pv_write(l->dst_type, l->dst_instance, out, l->priority);

	if (rc < 0) {
		if (l->write_err >= 0) {
			uc_logf(UC_LOG_WRN, "l%u: write %s:%u failed (%d)", (unsigned int)i,
				uc_obj_type_abbr(l->dst_type), (unsigned int)l->dst_instance,
				(int)rc);
		}
		l->write_err = rc;
		l->have_out = false;
		return;
	}
	if (l->write_err < 0) {
		uc_logf(UC_LOG_INF, "l%u: write ok again", (unsigned int)i);
	}
	l->write_err = 0;
	l->have_out = true;
	l->last_out = out;
	l->writes++;
}

static int32_t src_read(const struct link *l, double *v)
{
	if (l->src_device == UC_DEVICE_LOCAL) {
		return uc_pv_read(l->src_type, l->src_instance, v);
	}
	/* the host serves the local device instance without network traffic */
	return uc_remote_read(l->src_device, l->src_type, l->src_instance, UC_PROP_PRESENT_VALUE,
			      UC_ARRAY_ALL, v, READ_TIMEOUT_MS);
}

static void poll_link(uint32_t i, struct link *l, uint64_t now)
{
	double v;
	int32_t rc;

	l->next_poll_ms = now + l->period_ms;
	rc = src_read(l, &v);
	if (rc < 0) {
		if (rc != l->read_err) {
			uc_logf(UC_LOG_WRN, "l%u: read %u %s:%u failed (%d)", (unsigned int)i,
				(unsigned int)l->src_device, uc_obj_type_abbr(l->src_type),
				(unsigned int)l->src_instance, (int)rc);
		}
		l->read_err = rc;
		return;
	}
	if (l->read_err < 0) {
		uc_logf(UC_LOG_INF, "l%u: read ok again", (unsigned int)i);
	}
	l->read_err = 0;
	if (!l->have_out || ((v * l->scale + l->offset) != l->last_out)) {
		dst_write(i, l, v);
	}
}

static void subscribe(uint32_t i, struct link *l, uint64_t now, bool retry)
{
	int32_t rc = uc_cov_subscribe(l->src_device, l->src_type, l->src_instance, COV_LIFETIME_S);

	if (rc >= 0) {
		l->sub_id = rc;
		if (retry) {
			uc_logf(UC_LOG_INF, "l%u: COV subscription active", (unsigned int)i);
		}
		return;
	}
	l->sub_id = -1;
	l->next_sub_ms = now + RESUBSCRIBE_MS;
	l->next_poll_ms = now;
	if (!retry) {
		uc_logf(UC_LOG_WRN, "l%u: COV subscription failed (%d), polling every %u ms",
			(unsigned int)i, (int)rc, (unsigned int)l->period_ms);
	}
}

/* Create or check the destination object. */
static int32_t dst_setup(uint32_t i, struct link *l)
{
	char name[16];
	double v;
	int32_t rc;

	if ((l->dst_type == UC_OBJ_ANALOG_VALUE) || (l->dst_type == UC_OBJ_BINARY_VALUE) ||
	    (l->dst_type == UC_OBJ_MULTI_STATE_VALUE)) {
		snprintf(name, sizeof(name), "link-%u", (unsigned int)i);
		rc = uc_obj_create_str(l->dst_type, l->dst_instance, name);
		if (rc == UC_OK) {
			l->created = true;
			return UC_OK;
		}
		if (rc == UC_ERR_EXISTS) {
			return UC_OK;
		}
	} else {
		rc = uc_pv_read(l->dst_type, l->dst_instance, &v);
		if (rc >= 0) {
			return UC_OK;
		}
	}
	uc_logf(UC_LOG_ERR, "l%u: destination %s:%u not usable (%d), skipped", (unsigned int)i,
		uc_obj_type_abbr(l->dst_type), (unsigned int)l->dst_instance, (int)rc);
	return rc;
}

/* ---------------------------------------------------------------------- */
/* Callbacks                                                               */
/* ---------------------------------------------------------------------- */

UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
	char key[4] = "l0";
	char value[UC_PARAM_VALUE_MAX + 1];
	uint64_t now = uc_uptime_ms();
	uint32_t tick = PERIOD_MAX_MS;
	const char *why;
	int32_t rc;

	memset(&g, 0, sizeof(g));
	if (uc_param_u32("count", 1, LINK_MAX, 0, &g.count) <= 0) {
		uc_log_str(UC_LOG_ERR, "param count missing or not 1..8");
		return UC_ERR_INVALID;
	}

	for (uint32_t i = 0; i < g.count; i++) {
		struct link *l = &g.links[i];

		key[1] = (char)('0' + i);
		rc = uc_param_text(key, value, sizeof(value));
		if (rc <= 0) {
			uc_logf(UC_LOG_ERR, "%s: missing, skipped", key);
			continue;
		}
		if (!link_parse(value, l, &why)) {
			uc_logf(UC_LOG_ERR, "%s: malformed (%s) \"%s\", skipped", key, why, value);
			l->valid = false;
			continue;
		}
		if (dst_setup(i, l) < 0) {
			l->valid = false;
			continue;
		}
		if (l->mode == MODE_COV) {
			subscribe(i, l, now, false);
		} else {
			l->next_poll_ms = now;
		}
		if (l->period_ms < tick) {
			tick = l->period_ms;
		}
		g.valid++;
		uc_logf(UC_LOG_DBG, "%s: %u %s:%u -> %s:%u %s %u ms @%u x%g %+g", key,
			(unsigned int)l->src_device, uc_obj_type_abbr(l->src_type),
			(unsigned int)l->src_instance, uc_obj_type_abbr(l->dst_type),
			(unsigned int)l->dst_instance, (l->mode == MODE_COV) ? "cov" : "poll",
			(unsigned int)l->period_ms, (unsigned int)l->priority, l->scale, l->offset);
	}

	if (g.valid == 0u) {
		uc_log_str(UC_LOG_ERR, "no usable link");
		return UC_ERR_INVALID;
	}
	rc = uc_set_tick_period(tick);
	if (rc < 0) {
		return rc;
	}
	uc_logf(UC_LOG_INF, "%u of %u links active, tick %u ms", (unsigned int)g.valid,
		(unsigned int)g.count, (unsigned int)tick);
	return 0;
}

UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now_ms)
{
	for (uint32_t i = 0; i < g.count; i++) {
		struct link *l = &g.links[i];

		if (!l->valid) {
			continue;
		}
		if (l->mode == MODE_COV) {
			if (l->sub_id >= 0) {
				continue;
			}
			if (now_ms >= l->next_sub_ms) {
				l->next_sub_ms = now_ms + RESUBSCRIBE_MS;
				subscribe(i, l, now_ms, true);
				if (l->sub_id >= 0) {
					continue;
				}
			}
		}
		if (now_ms >= l->next_poll_ms) {
			poll_link(i, l, now_ms);
		}
	}
}

UC_EXPORT(uc_app_on_cov)
void uc_app_on_cov(int32_t sub_id, uint32_t device, uint32_t type, uint32_t instance, uint32_t prop,
		   double value)
{
	(void)device;
	(void)prop;
	for (uint32_t i = 0; i < g.count; i++) {
		struct link *l = &g.links[i];

		if (l->valid && (l->sub_id == sub_id) && (l->src_type == type) &&
		    (l->src_instance == instance)) {
			dst_write(i, l, value);
		}
	}
}

UC_EXPORT(uc_app_deinit) void uc_app_deinit(void)
{
	for (uint32_t i = 0; i < g.count; i++) {
		struct link *l = &g.links[i];

		if (!l->valid) {
			continue;
		}
		if (l->sub_id >= 0) {
			(void)uc_cov_unsubscribe(l->sub_id);
			l->sub_id = -1;
		}
		if (!l->created && (l->priority != UC_PRIORITY_NONE)) {
			(void)uc_prop_write_null(l->dst_type, l->dst_instance,
						 UC_PROP_PRESENT_VALUE, l->priority);
		}
	}
	uc_log_str(UC_LOG_INF, "stopped");
}

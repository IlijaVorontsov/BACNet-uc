/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * alarm - limit alarm with hysteresis and on/off delays on a local or
 * remote point, published as a binary-value, with a persistent trip
 * counter.
 *
 * Parameters (apps.json "params", all optional):
 *   src_device     "local" (default) or a device instance
 *   src_type       default 0 (analog-input)    src_instance  default 1
 *   direction      "high" (default): alarm when value > threshold,
 *                  cleared when value < threshold - hysteresis;
 *                  "low": alarm when value < threshold, cleared when
 *                  value > threshold + hysteresis
 *   threshold      default 30.0                hysteresis    default 1.0
 *   delay_ms       condition must hold this long to trip, default 0
 *   clear_delay_ms condition must hold this long to clear, default 0
 *   bv_instance    alarm binary-value created by the app, default 10
 *   name           its object name, default "alarm"
 *   count_instance if set: analog-value showing the trip counter;
 *                  writing 0 to it resets the counter
 *   poll_ms        poll the point after this long without a value, 10000
 *   stale_ms       warn when the value is older than this, default 60000
 *
 * The trip counter and the alarm state are stored with uc_kv_set() under
 * the key "state" (12 bytes: "ALM1", trips u32 LE, active u8, 3 pad),
 * written on every change. After a restart the alarm resumes in the
 * stored state, so a condition that persists across the restart is not
 * counted twice. Without permission "kv" the counter is kept in RAM.
 * A stale point keeps the current alarm state.
 *
 * Permissions: bacnet.local; bacnet.remote for a remote point; kv.
 */

#include <bacnet_uc.h>
#include <uc_point.h>
#include <uc_util.h>

UC_APP_DECLARE()

#define KV_KEY     "state"
#define KV_SIZE    12
#define KV_MAGIC_0 'A'
#define KV_MAGIC_1 'L'
#define KV_MAGIC_2 'M'
#define KV_MAGIC_3 '1'

enum {
	DIR_HIGH,
	DIR_LOW
};

static struct {
	struct uc_point src;
	uint32_t direction;
	double threshold;
	double hysteresis;
	uint32_t delay_ms;
	uint32_t clear_delay_ms;
	uint32_t bv_instance;
	bool has_count;
	uint32_t count_instance;
	uint32_t stale_ms;
	/* state */
	bool active;
	uint32_t trips;
	bool pending; /* the opposite condition holds since pending_ms */
	uint64_t pending_ms;
	bool kv; /* persistence available */
	bool stale;
	int32_t read_err;
} g;

/* ---------------------------------------------------------------------- */
/* Persistence                                                             */
/* ---------------------------------------------------------------------- */

static void state_save(void)
{
	uint8_t buf[KV_SIZE] = {KV_MAGIC_0, KV_MAGIC_1, KV_MAGIC_2, KV_MAGIC_3};
	int32_t rc;

	if (!g.kv) {
		return;
	}
	for (int i = 0; i < 4; i++) {
		buf[4 + i] = (uint8_t)(g.trips >> (8 * i));
	}
	buf[8] = g.active ? 1u : 0u;
	rc = uc_kv_set(KV_KEY, sizeof(KV_KEY) - 1, buf, sizeof(buf));
	if (rc < 0) {
		uc_logf(UC_LOG_WRN, "state not saved (%d)", (int)rc);
	}
}

static void state_load(void)
{
	uint8_t buf[KV_SIZE];
	int32_t rc = uc_kv_get(KV_KEY, sizeof(KV_KEY) - 1, buf, sizeof(buf));

	g.kv = (rc != UC_ERR_PERM) && (rc != UC_ERR_UNSUPPORTED);
	if (!g.kv) {
		uc_log_str(UC_LOG_INF, "no kv permission: trip counter not persistent");
		return;
	}
	if (rc == UC_ERR_NOT_FOUND) {
		return;
	}
	if ((rc != KV_SIZE) || (buf[0] != KV_MAGIC_0) || (buf[1] != KV_MAGIC_1) ||
	    (buf[2] != KV_MAGIC_2) || (buf[3] != KV_MAGIC_3)) {
		uc_logf(UC_LOG_WRN, "stored state ignored (%d)", (int)rc);
		return;
	}
	g.trips = (uint32_t)buf[4] | ((uint32_t)buf[5] << 8) | ((uint32_t)buf[6] << 16) |
		  ((uint32_t)buf[7] << 24);
	g.active = buf[8] != 0u;
}

/* ---------------------------------------------------------------------- */
/* Outputs                                                                 */
/* ---------------------------------------------------------------------- */

static void publish(void)
{
	int32_t rc = uc_pv_write(UC_OBJ_BINARY_VALUE, g.bv_instance, g.active ? 1.0 : 0.0,
				 UC_PRIORITY_NONE);

	if (rc < 0) {
		uc_logf(UC_LOG_WRN, "BV:%u write failed (%d)", (unsigned int)g.bv_instance,
			(int)rc);
	}
	if (g.has_count) {
		(void)uc_pv_write(UC_OBJ_ANALOG_VALUE, g.count_instance, (double)g.trips,
				  UC_PRIORITY_NONE);
	}
}

/* ---------------------------------------------------------------------- */
/* Evaluation                                                              */
/* ---------------------------------------------------------------------- */

/* true when the value calls for the opposite of the current state */
static bool transition_wanted(double v)
{
	if (g.direction == DIR_HIGH) {
		return g.active ? (v < g.threshold - g.hysteresis) : (v > g.threshold);
	}
	return g.active ? (v > g.threshold + g.hysteresis) : (v < g.threshold);
}

static void evaluate(uint64_t now)
{
	double v = g.src.value;
	uint32_t delay;

	if (!uc_point_fresh(&g.src, now, g.stale_ms)) {
		if (!g.stale) {
			uc_logf(UC_LOG_WRN, "%s:%u: no value for %u ms (err %d), state kept",
				uc_obj_type_abbr(g.src.type), (unsigned int)g.src.instance,
				(unsigned int)g.stale_ms, (int)g.src.read_err);
			g.stale = true;
		}
		g.pending = false;
		return;
	}
	if (g.stale) {
		uc_logf(UC_LOG_INF, "%s:%u: value %.2f again", uc_obj_type_abbr(g.src.type),
			(unsigned int)g.src.instance, v);
		g.stale = false;
	}

	if (!transition_wanted(v)) {
		g.pending = false;
		return;
	}
	if (!g.pending) {
		g.pending = true;
		g.pending_ms = now;
	}
	delay = g.active ? g.clear_delay_ms : g.delay_ms;
	if ((now - g.pending_ms) < delay) {
		return;
	}

	g.pending = false;
	g.active = !g.active;
	if (g.active) {
		g.trips++;
		uc_logf(UC_LOG_WRN, "ALARM %s:%u = %.2f %s %.2f (trip %u)",
			uc_obj_type_abbr(g.src.type), (unsigned int)g.src.instance, v,
			(g.direction == DIR_HIGH) ? ">" : "<", g.threshold, (unsigned int)g.trips);
	} else {
		uc_logf(UC_LOG_INF, "cleared %s:%u = %.2f", uc_obj_type_abbr(g.src.type),
			(unsigned int)g.src.instance, v);
	}
	publish();
	state_save();
}

/* ---------------------------------------------------------------------- */
/* Callbacks                                                               */
/* ---------------------------------------------------------------------- */

UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
	static const char *const dirs[] = {"high", "low", NULL};
	char name[UC_PARAM_VALUE_MAX + 1];
	char dbuf[12];
	uint64_t now = uc_uptime_ms();
	uint32_t dev, type, inst, poll_ms;
	int32_t rc;

	memset(&g, 0, sizeof(g));
	(void)uc_param_device("src_device", UC_DEVICE_LOCAL, &dev);
	(void)uc_param_u32("src_type", 0, UC_OBJ_TYPE_MAX, UC_OBJ_ANALOG_INPUT, &type);
	(void)uc_param_u32("src_instance", 0, UC_INSTANCE_MAX, 1, &inst);
	(void)uc_param_choice("direction", dirs, DIR_HIGH, &g.direction);
	(void)uc_param_double("threshold", -1e12, 1e12, 30.0, &g.threshold);
	(void)uc_param_double("hysteresis", 0.0, 1e12, 1.0, &g.hysteresis);
	(void)uc_param_u32("delay_ms", 0, 86400000, 0, &g.delay_ms);
	(void)uc_param_u32("clear_delay_ms", 0, 86400000, 0, &g.clear_delay_ms);
	(void)uc_param_u32("bv_instance", 0, UC_INSTANCE_MAX, 10, &g.bv_instance);
	g.has_count = uc_param_u32("count_instance", 0, UC_INSTANCE_MAX, 0, &g.count_instance) > 0;
	(void)uc_param_u32("poll_ms", 100, 3600000, 10000, &poll_ms);
	(void)uc_param_u32("stale_ms", 1000, 86400000, 60000, &g.stale_ms);
	if (uc_param_text("name", name, sizeof(name)) <= 0) {
		memcpy(name, "alarm", sizeof("alarm"));
	}

	state_load();

	rc = uc_obj_create_str(UC_OBJ_BINARY_VALUE, g.bv_instance, name);
	if (rc < 0) {
		uc_logf(UC_LOG_ERR, "BV:%u: create failed (%d)", (unsigned int)g.bv_instance,
			(int)rc);
		return rc;
	}
	if (g.has_count) {
		rc = uc_obj_create_str(UC_OBJ_ANALOG_VALUE, g.count_instance, "alarm-trips");
		if (rc < 0) {
			uc_logf(UC_LOG_WRN, "AV:%u: create failed (%d), no counter object",
				(unsigned int)g.count_instance, (int)rc);
			g.has_count = false;
		}
	}
	publish();

	uc_point_setup(&g.src, dev, type, inst, poll_ms);
	rc = uc_point_start(&g.src, now);
	if (rc < 0) {
		uc_logf(UC_LOG_WRN, "COV subscription failed (%d), polling every %u ms", (int)rc,
			(unsigned int)poll_ms);
	}
	uc_logf(UC_LOG_INF, "%s:%u@%s %s %.2f hyst %.2f -> BV:%u (%s, %u trips)",
		uc_obj_type_abbr(type), (unsigned int)inst, uc_device_str(dev, dbuf, sizeof(dbuf)),
		(g.direction == DIR_HIGH) ? ">" : "<", g.threshold, g.hysteresis,
		(unsigned int)g.bv_instance, g.active ? "active" : "normal", (unsigned int)g.trips);
	return 0;
}

UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now_ms)
{
	int32_t rc = uc_point_tick(&g.src, now_ms);

	if ((rc < 0) && (g.read_err >= 0)) {
		uc_logf(UC_LOG_WRN, "read failed (%d)", (int)rc);
	}
	if (rc != 0) {
		g.read_err = rc;
	}
	evaluate(now_ms);
}

UC_EXPORT(uc_app_on_cov)
void uc_app_on_cov(int32_t sub_id, uint32_t device, uint32_t type, uint32_t instance, uint32_t prop,
		   double value)
{
	uint64_t now = uc_uptime_ms();

	(void)device;
	(void)prop;
	if (uc_point_on_cov(&g.src, sub_id, type, instance, value, now)) {
		g.read_err = 0;
		evaluate(now);
	}
}

UC_EXPORT(uc_app_on_write)
void uc_app_on_write(uint32_t type, uint32_t instance, uint32_t prop, uint32_t priority,
		     double value)
{
	if (prop != UC_PROP_PRESENT_VALUE) {
		return;
	}
	if (g.has_count && (type == UC_OBJ_ANALOG_VALUE) && (instance == g.count_instance)) {
		if (value == 0.0) {
			uc_logf(UC_LOG_INF, "trip counter reset (was %u)", (unsigned int)g.trips);
			g.trips = 0;
			state_save();
		}
		/* the counter object always shows the counter (an analog-value
		 * has no priority array: this overwrites any written value) */
		(void)uc_pv_write(UC_OBJ_ANALOG_VALUE, g.count_instance, (double)g.trips,
				  UC_PRIORITY_NONE);
	} else if ((type == UC_OBJ_BINARY_VALUE) && (instance == g.bv_instance)) {
		uc_logf(UC_LOG_INF, "BV:%u overridden: %s @%u", (unsigned int)g.bv_instance,
			(value != 0.0) ? "active" : "inactive", (unsigned int)priority);
	}
}

UC_EXPORT(uc_app_deinit) void uc_app_deinit(void)
{
	uc_point_stop(&g.src);
	uc_logf(UC_LOG_INF, "stopped (%s, %u trips)", g.active ? "active" : "normal",
		(unsigned int)g.trips);
}

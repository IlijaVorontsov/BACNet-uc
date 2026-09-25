/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * thermostat - PI room temperature controller.
 *
 * Reads the room temperature from a local or remote object (COV
 * subscription, polled when no value arrived for poll_ms), provides a
 * writable setpoint analog-value and drives an analog output (0..100 %),
 * or a binary output by time-proportioning.
 *
 * Parameters (apps.json "params", all optional; devices are "local" or a
 * device instance):
 *   sensor_device   default local      sensor_type     default 0 (AI)
 *   sensor_instance default 1
 *   setpoint        initial setpoint, default 21.0 (a setpoint stored
 *                   with permission "kv" takes precedence)
 *   sp_instance     setpoint analog-value created by the app, default 1
 *   sp_min, sp_max  accepted setpoint range, default 5..35
 *   out_device      default local      out_type        default 1 (AO)
 *   out_instance    default 1          out_priority    default 0 (none)
 *   action          "heat" (default): output rises when too cold,
 *                   "cool": output rises when too warm
 *   kp              proportional gain in %/K, default 20
 *   ti_s            integral time in s, default 600 (0: P only)
 *   out_min, out_max output limits in %, default 0..100
 *   out_deadband    min. change before an analog output is rewritten, 0.5
 *   refresh_ms      rewrite an unchanged output after this, default 60000
 *   pwm_ms          time-proportioning cycle for binary outputs, 600000
 *   poll_ms         poll the sensor after this long without a value, 10000
 *   stale_ms        sensor value older than this: fail-safe, default 60000
 *   fail_output     output while the sensor is stale, default 0 (%)
 *   status_s        period of the status log line, default 60 (0: off)
 *   timeout_ms      remote request timeout, default 2000
 *
 * Control (every tick, dt from uc_uptime_ms):
 *   e = sp - pv (heat) or pv - sp (cool)
 *   P = kp * e
 *   I += kp * e * dt / ti_s   unless the output is saturated in the
 *                             direction of e (conditional integration);
 *                             I is kept within [out_min, out_max]
 *   u = clamp(P + I, out_min, out_max)
 * A setpoint written by a BACnet client arrives through uc_app_on_write;
 * the effective Present_Value (priority array) is re-read every tick, so
 * a relinquish is noticed as well. With permission "kv" the setpoint is
 * persisted and survives restarts.
 *
 * Permissions: bacnet.local; bacnet.remote for remote sensor/output;
 * kv (optional) for setpoint persistence.
 */

#include <bacnet_uc.h>
#include <uc_point.h>
#include <uc_util.h>

UC_APP_DECLARE()

#define UNITS_DEGREES_CELSIUS 62.0
#define KV_SETPOINT           "setpoint"

enum {
	ACTION_HEAT,
	ACTION_COOL
};

static struct {
	/* configuration */
	struct uc_point sensor;
	uint32_t sp_instance;
	double sp_min;
	double sp_max;
	uint32_t out_device;
	uint32_t out_type;
	uint32_t out_instance;
	uint32_t out_priority;
	bool out_binary;
	uint32_t action;
	double kp;
	double ti_s;
	double out_min;
	double out_max;
	double out_deadband;
	uint32_t refresh_ms;
	uint32_t pwm_ms;
	uint32_t stale_ms;
	double fail_output;
	uint32_t status_ms;
	uint32_t timeout_ms;
	/* state */
	double sp;       /* effective setpoint (clamped) */
	double sp_raw;   /* last Present_Value read from the object */
	bool kv;         /* persistence available */
	double integral; /* % */
	double output;   /* % */
	double p_term;
	bool written_valid;
	double written; /* last value written (%, or 0/1 for binary) */
	uint64_t written_ms;
	int32_t out_err;
	uint64_t last_ms;
	bool have_last;
	uint64_t pwm_start_ms;
	bool stale;
	uint64_t next_status_ms;
	uint32_t sensor_err_logged;
} g;

/* ---------------------------------------------------------------------- */
/* Setpoint                                                                */
/* ---------------------------------------------------------------------- */

static void sp_persist(double sp)
{
	int32_t rc;

	if (!g.kv) {
		return;
	}
	rc = uc_kv_set(KV_SETPOINT, sizeof(KV_SETPOINT) - 1, &sp, sizeof(sp));
	if (rc < 0) {
		uc_logf(UC_LOG_WRN, "setpoint not saved (%d)", (int)rc);
	}
}

/* Stored setpoint, if any. Also decides whether persistence is used. */
static bool sp_load(double *sp)
{
	double v;
	int32_t rc = uc_kv_get(KV_SETPOINT, sizeof(KV_SETPOINT) - 1, &v, sizeof(v));

	g.kv = (rc != UC_ERR_PERM) && (rc != UC_ERR_UNSUPPORTED);
	if ((rc == (int32_t)sizeof(v)) && uc_isfinite(v)) {
		*sp = v;
		return true;
	}
	if ((rc >= 0) || ((rc != UC_ERR_NOT_FOUND) && g.kv)) {
		uc_logf(UC_LOG_WRN, "stored setpoint ignored (%d)", (int)rc);
	}
	return false;
}

/* Apply the effective setpoint read from the object. */
static void sp_update(double raw, const char *why)
{
	double sp = uc_clamp(raw, g.sp_min, g.sp_max);

	if (raw == g.sp_raw) {
		return;
	}
	g.sp_raw = raw;
	if (sp != raw) {
		uc_logf(UC_LOG_WRN, "setpoint %.2f outside %.1f..%.1f, using %.2f", raw, g.sp_min,
			g.sp_max, sp);
	}
	if (sp != g.sp) {
		uc_logf(UC_LOG_INF, "setpoint %.2f -> %.2f (%s)", g.sp, sp, why);
		g.sp = sp;
		sp_persist(sp);
	}
}

static void sp_refresh(const char *why)
{
	double v;

	if (uc_pv_read(UC_OBJ_ANALOG_VALUE, g.sp_instance, &v) == UC_OK) {
		sp_update(v, why);
	}
}

/* ---------------------------------------------------------------------- */
/* Output                                                                  */
/* ---------------------------------------------------------------------- */

static void out_write(double v, uint64_t now)
{
	int32_t rc = uc_pv_write_dev(g.out_device, g.out_type, g.out_instance, v, g.out_priority,
				     g.timeout_ms);

	if (rc < 0) {
		if (g.out_err >= 0) {
			uc_logf(UC_LOG_WRN, "output %s:%u write failed (%d)",
				uc_obj_type_abbr(g.out_type), (unsigned int)g.out_instance,
				(int)rc);
		}
		g.out_err = rc;
		g.written_valid = false;
		return;
	}
	if (g.out_err < 0) {
		uc_log_str(UC_LOG_INF, "output write ok again");
	}
	g.out_err = 0;
	g.written = v;
	g.written_valid = true;
	g.written_ms = now;
}

/* Write the output (% of g.output) when it changed enough or is due. */
static void out_apply(uint64_t now)
{
	bool refresh = !g.written_valid || ((now - g.written_ms) >= g.refresh_ms);
	double v;

	if (g.out_binary) {
		/* time-proportioning: on for output % of every pwm cycle */
		uint64_t phase = (now - g.pwm_start_ms) % g.pwm_ms;
		double on_ms = g.output / 100.0 * (double)g.pwm_ms;

		v = ((double)phase < on_ms) ? 1.0 : 0.0;
		if (refresh || (v != g.written)) {
			out_write(v, now);
		}
		return;
	}
	v = g.output;
	if (refresh || (uc_fabs(v - g.written) >= g.out_deadband) ||
	    ((v != g.written) && ((v == g.out_min) || (v == g.out_max)))) {
		out_write(v, now);
	}
}

/* ---------------------------------------------------------------------- */
/* Control                                                                 */
/* ---------------------------------------------------------------------- */

static void control_step(uint64_t now)
{
	double dt = 0.0;
	double e;
	double u;

	if (g.have_last && (now > g.last_ms)) {
		dt = (double)(now - g.last_ms) / 1000.0;
	}
	g.last_ms = now;
	g.have_last = true;

	if (!uc_point_fresh(&g.sensor, now, g.stale_ms)) {
		if (!g.stale) {
			uc_logf(UC_LOG_WRN, "no sensor value for %u ms (err %d), output %.1f",
				(unsigned int)g.stale_ms, (int)g.sensor.read_err, g.fail_output);
			g.stale = true;
		}
		g.integral = 0.0;
		g.p_term = 0.0;
		g.output = g.fail_output;
		return;
	}
	if (g.stale) {
		uc_logf(UC_LOG_INF, "sensor value %.2f, control resumed", g.sensor.value);
		g.stale = false;
	}

	e = (g.action == ACTION_HEAT) ? (g.sp - g.sensor.value) : (g.sensor.value - g.sp);
	g.p_term = g.kp * e;
	if ((g.ti_s > 0.0) && (dt > 0.0)) {
		double i_new = g.integral + g.kp * e * dt / g.ti_s;
		double u_try = g.p_term + i_new;

		/* anti-windup: no integration further into saturation */
		if (!((u_try > g.out_max) && (e > 0.0)) && !((u_try < g.out_min) && (e < 0.0))) {
			g.integral = uc_clamp(i_new, g.out_min, g.out_max);
		}
	}
	u = uc_clamp(g.p_term + g.integral, g.out_min, g.out_max);
	g.output = u;
}

static void status_log(uint64_t now)
{
	if ((g.status_ms == 0u) || (now < g.next_status_ms)) {
		return;
	}
	g.next_status_ms = now + g.status_ms;
	if (g.stale) {
		uc_logf(UC_LOG_INF, "T=? SP=%.2f out=%.1f%% (fail-safe)", g.sp, g.output);
	} else {
		uc_logf(UC_LOG_INF, "T=%.2f SP=%.2f out=%.1f%% P=%.1f I=%.1f", g.sensor.value, g.sp,
			g.output, g.p_term, g.integral);
	}
}

/* ---------------------------------------------------------------------- */
/* Callbacks                                                               */
/* ---------------------------------------------------------------------- */

static void read_params(uint32_t *sensor_dev, uint32_t *sensor_type, uint32_t *sensor_inst,
			uint32_t *poll_ms, double *sp)
{
	static const char *const actions[] = {"heat", "cool", NULL};
	uint32_t status_s;

	(void)uc_param_device("sensor_device", UC_DEVICE_LOCAL, sensor_dev);
	(void)uc_param_u32("sensor_type", 0, UC_OBJ_TYPE_MAX, UC_OBJ_ANALOG_INPUT, sensor_type);
	(void)uc_param_u32("sensor_instance", 0, UC_INSTANCE_MAX, 1, sensor_inst);
	(void)uc_param_u32("poll_ms", 100, 3600000, 10000, poll_ms);
	(void)uc_param_u32("stale_ms", 1000, 86400000, 60000, &g.stale_ms);
	(void)uc_param_u32("timeout_ms", 100, 60000, 2000, &g.timeout_ms);

	(void)uc_param_double("sp_min", -100.0, 1000.0, 5.0, &g.sp_min);
	(void)uc_param_double("sp_max", g.sp_min, 1000.0, 35.0, &g.sp_max);
	(void)uc_param_double("setpoint", g.sp_min, g.sp_max, 21.0, sp);
	(void)uc_param_u32("sp_instance", 0, UC_INSTANCE_MAX, 1, &g.sp_instance);

	(void)uc_param_device("out_device", UC_DEVICE_LOCAL, &g.out_device);
	(void)uc_param_u32("out_type", 0, UC_OBJ_TYPE_MAX, UC_OBJ_ANALOG_OUTPUT, &g.out_type);
	(void)uc_param_u32("out_instance", 0, UC_INSTANCE_MAX, 1, &g.out_instance);
	(void)uc_param_u32("out_priority", 0, 16, UC_PRIORITY_NONE, &g.out_priority);
	(void)uc_param_choice("action", actions, ACTION_HEAT, &g.action);

	(void)uc_param_double("kp", 0.0, 1e6, 20.0, &g.kp);
	(void)uc_param_double("ti_s", 0.0, 1e6, 600.0, &g.ti_s);
	(void)uc_param_double("out_min", -1e6, 1e6, 0.0, &g.out_min);
	(void)uc_param_double("out_max", g.out_min, 1e6, 100.0, &g.out_max);
	(void)uc_param_double("out_deadband", 0.0, 1e6, 0.5, &g.out_deadband);
	(void)uc_param_u32("refresh_ms", 1000, 86400000, 60000, &g.refresh_ms);
	(void)uc_param_u32("pwm_ms", 1000, 86400000, 600000, &g.pwm_ms);
	(void)uc_param_double("fail_output", g.out_min, g.out_max, g.out_min, &g.fail_output);
	(void)uc_param_u32("status_s", 0, 86400, 60, &status_s);
	g.status_ms = status_s * 1000u;
}

UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
	uint64_t now = uc_uptime_ms();
	uint32_t dev, type, inst, poll_ms;
	char dbuf[2][12];
	double sp0;
	double stored;
	int32_t rc;

	memset(&g, 0, sizeof(g));
	read_params(&dev, &type, &inst, &poll_ms, &sp0);
	g.out_binary = uc_obj_is_binary(g.out_type);

	/* setpoint object, initialised from kv or the parameter */
	if (sp_load(&stored)) {
		sp0 = uc_clamp(stored, g.sp_min, g.sp_max);
	}
	rc = uc_obj_create_str(UC_OBJ_ANALOG_VALUE, g.sp_instance, "thermostat-setpoint");
	if (rc < 0) {
		uc_logf(UC_LOG_ERR, "setpoint AV:%u: create failed (%d)",
			(unsigned int)g.sp_instance, (int)rc);
		return rc;
	}
	(void)uc_prop_write(UC_OBJ_ANALOG_VALUE, g.sp_instance, UC_PROP_UNITS, UC_ARRAY_ALL,
			    UNITS_DEGREES_CELSIUS, UC_PRIORITY_NONE);
	rc = uc_pv_write(UC_OBJ_ANALOG_VALUE, g.sp_instance, sp0, UC_PRIORITY_NONE);
	if (rc < 0) {
		uc_logf(UC_LOG_ERR, "setpoint AV:%u: write failed (%d)",
			(unsigned int)g.sp_instance, (int)rc);
		return rc;
	}
	/* the object stores a REAL: start from the value as stored */
	if (uc_pv_read(UC_OBJ_ANALOG_VALUE, g.sp_instance, &stored) == UC_OK) {
		sp0 = stored;
	}
	g.sp = uc_clamp(sp0, g.sp_min, g.sp_max);
	g.sp_raw = sp0;

	/* sensor: COV, polling fallback */
	uc_point_setup(&g.sensor, dev, type, inst, poll_ms);
	g.sensor.timeout_ms = g.timeout_ms;
	rc = uc_point_start(&g.sensor, now);
	if (rc < 0) {
		uc_logf(UC_LOG_WRN, "COV subscription failed (%d), polling every %u ms", (int)rc,
			(unsigned int)poll_ms);
	}

	g.out_err = 0;
	g.pwm_start_ms = now;
	g.next_status_ms = now + g.status_ms;
	g.output = g.fail_output;
	uc_logf(UC_LOG_INF, "sensor %s:%u@%s, SP %.2f (AV:%u), output %s:%u@%s, kp %.2f ti %.0f s",
		uc_obj_type_abbr(type), (unsigned int)inst, uc_device_str(dev, dbuf[0], 12), g.sp,
		(unsigned int)g.sp_instance, uc_obj_type_abbr(g.out_type),
		(unsigned int)g.out_instance, uc_device_str(g.out_device, dbuf[1], 12), g.kp,
		g.ti_s);
	return 0;
}

UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now_ms)
{
	int32_t rc = uc_point_tick(&g.sensor, now_ms);

	if ((rc < 0) && (g.sensor_err_logged == 0u)) {
		uc_logf(UC_LOG_WRN, "sensor read failed (%d)", (int)rc);
		g.sensor_err_logged = 1;
	} else if (rc > 0) {
		g.sensor_err_logged = 0;
	}
	sp_refresh("priority array");
	control_step(now_ms);
	out_apply(now_ms);
	status_log(now_ms);
}

UC_EXPORT(uc_app_on_cov)
void uc_app_on_cov(int32_t sub_id, uint32_t device, uint32_t type, uint32_t instance, uint32_t prop,
		   double value)
{
	(void)device;
	(void)prop;
	if (uc_point_on_cov(&g.sensor, sub_id, type, instance, value, uc_uptime_ms())) {
		g.sensor_err_logged = 0;
		uc_logf(UC_LOG_DBG, "T=%.2f", value);
	}
}

UC_EXPORT(uc_app_on_write)
void uc_app_on_write(uint32_t type, uint32_t instance, uint32_t prop, uint32_t priority,
		     double value)
{
	char why[24];

	if ((type != UC_OBJ_ANALOG_VALUE) || (instance != g.sp_instance) ||
	    (prop != UC_PROP_PRESENT_VALUE)) {
		return;
	}
	snprintf(why, sizeof(why), "written @%u", (unsigned int)priority);
	(void)value;
	sp_refresh(why);
}

UC_EXPORT(uc_app_deinit) void uc_app_deinit(void)
{
	uc_point_stop(&g.sensor);
	if (g.out_priority != UC_PRIORITY_NONE) {
		/* hand the output back to lower priorities */
		(void)uc_pv_relinquish_dev(g.out_device, g.out_type, g.out_instance, g.out_priority,
					   g.timeout_ms);
	} else {
		/* no priority to give back: leave the output in the fail-safe state */
		(void)uc_pv_write_dev(g.out_device, g.out_type, g.out_instance,
				      g.out_binary ? 0.0 : g.fail_output, UC_PRIORITY_NONE,
				      g.timeout_ms);
	}
	uc_log_str(UC_LOG_INF, "stopped");
}

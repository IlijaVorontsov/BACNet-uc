/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * thermostat.c - PI room thermostat for the uc-hub demo site.
 *
 * Drives analog-output:1 (valve, %) at priority 12 from analog-input:1 (room
 * temperature) towards the setpoint in analog-value:1, which the app creates
 * with the "setpoint" parameter as relinquish default. Parameters: setpoint
 * (degrees C, default 21), kp (%/K, default 10), ki (%/(K*s), default 0.011).
 * The hub simulator emulates the same behaviour (uc_hub.sim.network).
 *
 * Build: ./build.sh (clang --target=wasm32, the recipe of bacnet_uc.h).
 */
#include "bacnet_uc.h"

#define VALVE_PRIORITY 12u

static double kp = 10.0;
static double ki = 0.011;
static double integral;
static uint64_t last_ms;

UC_APP_DECLARE()

static double clamp(double v, double lo, double hi)
{
	return v < lo ? lo : (v > hi ? hi : v);
}

UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
	int32_t rc;

	kp = uc_param_num("kp", kp);
	ki = uc_param_num("ki", ki);
	rc = uc_obj_create_str(UC_OBJ_ANALOG_VALUE, 1u, "Setpoint");
	if (rc < 0 && rc != UC_ERR_EXISTS) {
		uc_log_str(UC_LOG_ERR, "cannot create the setpoint object");
		return rc;
	}
	uc_prop_write(UC_OBJ_ANALOG_VALUE, 1u, UC_PROP_RELINQUISH_DEFAULT, UC_ARRAY_ALL,
		      uc_param_num("setpoint", 21.0), UC_PRIORITY_NONE);
	return UC_OK;
}

UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now_ms)
{
	double temp, setpoint, error, dt;

	dt = last_ms ? (double)(now_ms - last_ms) / 1000.0 : 0.0;
	last_ms = now_ms;
	if (uc_pv_read(UC_OBJ_ANALOG_INPUT, 1u, &temp) < 0 ||
	    uc_pv_read(UC_OBJ_ANALOG_VALUE, 1u, &setpoint) < 0) {
		return;
	}
	error = setpoint - temp;
	integral = clamp(integral + ki * error * dt, 0.0, 100.0);
	if (uc_pv_write(UC_OBJ_ANALOG_OUTPUT, 1u, clamp(kp * error + integral, 0.0, 100.0),
			VALVE_PRIORITY) < 0) {
		uc_log_str(UC_LOG_WRN, "valve write failed");
	}
}

UC_EXPORT(uc_app_deinit) void uc_app_deinit(void)
{
	uc_prop_write_null(UC_OBJ_ANALOG_OUTPUT, 1u, UC_PROP_PRESENT_VALUE, VALVE_PRIORITY);
}

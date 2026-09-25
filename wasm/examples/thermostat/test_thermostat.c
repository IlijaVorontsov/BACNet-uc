/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host-stub tests of the thermostat example (compiled natively together
 * with thermostat.c, sdk/host-stub/uc_stub.c and uc_stub_native.c).
 */

#include <math.h>

#include "uc_test.h"

#define AI  UC_OBJ_ANALOG_INPUT
#define AO  UC_OBJ_ANALOG_OUTPUT
#define AV  UC_OBJ_ANALOG_VALUE
#define BO  UC_OBJ_BINARY_OUTPUT
#define PV  UC_PROP_PRESENT_VALUE
#define DEV 1001u

/* Local sensor AI:1 and output AO:1 as io.json would create them. */
static void local_io(double temp)
{
	UC_CHECK_INT(uc_stub_obj_add(AI, 1, "Room Temperature", temp), 0);
	UC_CHECK_INT(uc_stub_obj_add(AO, 1, "Valve Position", 0.0), 0);
}

static void test_defaults_local_sensor(void)
{
	double v;

	local_io(18.0);
	UC_CHECK_INT(uc_stub_start(), 0);

	UC_CHECK(uc_stub_obj_app_owned(AV, 1));
	UC_CHECK(strcmp(uc_stub_obj_name(AV, 1), "thermostat-setpoint") == 0);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 1), 21.0, 1e-6);
	UC_CHECK_NEAR(uc_stub_obj_prop(AV, 1, UC_PROP_UNITS), 62.0, 0);
	UC_CHECK(uc_stub_sub_find(UC_DEVICE_LOCAL, AI, 1) >= 0);

	/* first tick after one period: P only, e = 3 K -> 60 % */
	uc_stub_run(999);
	UC_CHECK_INT(uc_stub_ticks(), 0);
	uc_stub_run(1);
	UC_CHECK_INT(uc_stub_ticks(), 1);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 60.0, 1e-4);
	UC_CHECK(uc_stub_obj_prio(AO, 1, 16, &v)); /* no priority -> 16 */

	/* integral: kp * e / ti = 20 * 3 / 600 = 0.1 %/s */
	uc_stub_run(20000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 62.0, 0.51);
	/* deadband 0.5 %: rewritten about every 5 s, not every tick */
	UC_CHECK(uc_stub_obj_writes(AO, 1) <= 6);
	UC_CHECK_INT(uc_stub_log_excess(), 0);
	UC_CHECK_INT(uc_stub_log_matches(UC_LOG_ERR, ""), 0);
}

static void test_remote_sensor_cov(void)
{
	uc_stub_param_set("sensor_device", "1001");
	uc_stub_param_set("setpoint", "21.5");
	uc_stub_obj_add(AO, 1, "Valve", 0.0);
	uc_stub_remote_add(DEV, AI, 1, 20.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK(uc_stub_sub_find(DEV, AI, 1) >= 0);

	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 30.0, 1e-4); /* 20 * 1.5 */

	/* warmer than the setpoint: output falls to 0 on the next tick */
	uc_stub_remote_set(DEV, AI, 1, 22.5, true);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 0.0, 1e-9);
	/* values arrive by COV: nothing polled so far */
	UC_CHECK_INT(uc_stub_remote_reads(DEV, AI, 1), 0);

	/* an unchanged value sends no notifications: polled after poll_ms */
	uc_stub_run(10000);
	UC_CHECK(uc_stub_remote_reads(DEV, AI, 1) >= 1);
	UC_CHECK_INT(uc_stub_log_matches(UC_LOG_WRN, ""), 0);
}

static void test_poll_fallback_when_subscribe_fails(void)
{
	uc_stub_param_set("sensor_device", "1001");
	uc_stub_param_set("poll_ms", "2000");
	local_io(0.0);
	uc_stub_remote_add(DEV, AI, 1, 19.0);
	uc_stub_fail(UC_STUB_FN_COV_SUBSCRIBE, UC_ERR_NO_MEM, 1);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_WRN, "COV subscription failed (-6), polling every 2000 ms", 1);
	UC_CHECK_INT(uc_stub_subs_active(), 0);

	uc_stub_run(1000);
	UC_CHECK_INT(uc_stub_remote_reads(DEV, AI, 1), 1);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 40.0, 1e-4);
	uc_stub_run(10000);
	UC_CHECK_INT(uc_stub_remote_reads(DEV, AI, 1), 6); /* t = 1, 3, 5, 7, 9, 11 s */

	/* the subscription is retried after 30 s */
	uc_stub_run(20000);
	UC_CHECK_INT(uc_stub_subs_active(), 1);
}

static void test_lost_notification_polled(void)
{
	uc_stub_param_set("sensor_device", "1001");
	uc_stub_param_set("poll_ms", "5000");
	local_io(0.0);
	uc_stub_remote_add(DEV, AI, 1, 20.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 20.0, 1e-4);

	/* value changes, notification lost: the poll picks it up */
	uc_stub_remote_set(DEV, AI, 1, 21.0, false);
	uc_stub_run(3000);
	UC_CHECK(uc_stub_obj_pv(AO, 1) > 19.0);
	uc_stub_run(3000);
	UC_CHECK(uc_stub_obj_pv(AO, 1) < 1.0);
	UC_CHECK_INT(uc_stub_remote_reads(DEV, AI, 1), 1);
}

static void test_stale_sensor_failsafe(void)
{
	uc_stub_param_set("sensor_device", "1001");
	uc_stub_param_set("fail_output", "10");
	local_io(0.0);
	uc_stub_remote_add(DEV, AI, 1, 18.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 60.0, 1e-4);

	/* device stops answering: after stale_ms the output goes to 10 % */
	uc_stub_remote_fail(DEV, AI, 1, UC_ERR_TIMEOUT);
	uc_stub_run(55000);
	UC_CHECK(uc_stub_obj_pv(AO, 1) > 55.0);
	uc_stub_run(20000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 10.0, 1e-4);
	UC_CHECK_LOG(UC_LOG_WRN, "no sensor value", 1);
	UC_CHECK_LOG(UC_LOG_WRN, "sensor read failed (-4)", 1);

	/* recovers with the next poll */
	uc_stub_remote_fail(DEV, AI, 1, 0);
	uc_stub_run(15000);
	UC_CHECK_LOG(UC_LOG_INF, "control resumed", 1);
	UC_CHECK(uc_stub_obj_pv(AO, 1) > 55.0);
	UC_CHECK_INT(uc_stub_log_excess(), 0);
}

static void test_setpoint_written_by_client(void)
{
	local_io(20.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 20.0, 1e-4);

	UC_CHECK_INT(uc_stub_client_write(AV, 1, PV, 23.0, 8), UC_OK);
	uc_stub_run(0);
	UC_CHECK_LOG(UC_LOG_INF, "setpoint 21.00 -> 23.00 (written @8)", 1);
	uc_stub_run(1000);
	UC_CHECK(uc_stub_obj_pv(AO, 1) > 59.0); /* e = 3 K */

	/* out of range: clamped to sp_max */
	uc_stub_client_write(AV, 1, PV, 50.0, 8);
	uc_stub_run(1000);
	UC_CHECK_LOG(UC_LOG_WRN, "setpoint 50.00 outside 5.0..35.0, using 35.00", 1);
	UC_CHECK(uc_stub_obj_pv(AO, 1) >= 99.99);

	/* relinquish: the analog-value has no priority array, the write
	 * succeeds and changes nothing */
	UC_CHECK_INT(uc_stub_client_relinquish(AV, 1, 8), UC_OK);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 1), 50.0, 0);
	UC_CHECK_LOG(UC_LOG_INF, "(present value)", 0);
	UC_CHECK(uc_stub_obj_pv(AO, 1) >= 99.99);

	/* any write sets it, whatever the priority */
	UC_CHECK_INT(uc_stub_client_write(AV, 1, PV, 21.0, 12), UC_OK);
	uc_stub_run(1000);
	UC_CHECK_LOG(UC_LOG_INF, "setpoint 35.00 -> 21.00 (written @12)", 1);
	UC_CHECK(uc_stub_obj_pv(AO, 1) < 25.0);
}

static void test_setpoint_persisted_with_kv(void)
{
	double stored = 0.0;

	uc_stub_param_set("setpoint", "21.5");
	local_io(20.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_client_write(AV, 1, PV, 24.0, 0);
	uc_stub_run(1000);
	UC_CHECK_INT(uc_stub_kv_get("setpoint", &stored, sizeof(stored)), sizeof(double));
	UC_CHECK_NEAR(stored, 24.0, 1e-9);

	uc_stub_stop();
	UC_CHECK(!uc_stub_obj_exists(AV, 1)); /* app objects are deleted */
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 1), 24.0, 1e-6);
}

static void test_no_kv_permission(void)
{
	uc_stub_set_perms(UC_STUB_PERM_LOCAL | UC_STUB_PERM_REMOTE);
	local_io(20.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_client_write(AV, 1, PV, 24.0, 0);
	uc_stub_run(1000);
	UC_CHECK_INT(uc_stub_kv_get("setpoint", NULL, 0), -1);
	UC_CHECK_LOG(UC_LOG_WRN, "", 0);
}

static void test_anti_windup(void)
{
	local_io(10.0);
	uc_stub_param_set("ti_s", "60");
	UC_CHECK_INT(uc_stub_start(), 0);
	/* e = 11 K: saturated at 100 % for ten minutes */
	uc_stub_run(600000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 100.0, 1e-9);

	/* slightly too warm: without anti-windup I would hold the output at
	 * 100 % for minutes; with it the output drops at once */
	uc_stub_obj_set_pv(AI, 1, 21.5);
	uc_stub_run(1000);
	UC_CHECK(uc_stub_obj_pv(AO, 1) < 1.0);
}

static void test_integral_limited(void)
{
	local_io(20.0);
	uc_stub_param_set("kp", "1");
	uc_stub_param_set("ti_s", "10");
	uc_stub_param_set("out_deadband", "0");
	UC_CHECK_INT(uc_stub_start(), 0);
	/* e = 1: I grows 0.1 %/s until P + I reaches out_max */
	uc_stub_run(2000000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 100.0, 1e-9);
	uc_stub_obj_set_pv(AI, 1, 22.0); /* e = -1 */
	uc_stub_run(1000);
	/* I stopped at 99 (P + I = 100), not at a wound-up value */
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 97.9, 0.15);
}

static void test_cooling_action(void)
{
	local_io(25.0);
	uc_stub_param_set("action", "cool");
	uc_stub_param_set("setpoint", "24");
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 20.0, 1e-4);
}

static void test_binary_output_time_proportioning(void)
{
	uc_stub_obj_add(AI, 1, "Room", 19.5);
	uc_stub_obj_add(BO, 1, "Heater Relay", 0.0);
	uc_stub_param_set("out_type", "4");
	uc_stub_param_set("ti_s", "0");
	uc_stub_param_set("pwm_ms", "10000");
	UC_CHECK_INT(uc_stub_start(), 0); /* e = 1.5 K -> 30 %: 3 s on, 7 s off */

	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BO, 1), 1.0, 0);
	uc_stub_run(2000); /* t = 3 s */
	UC_CHECK_NEAR(uc_stub_obj_pv(BO, 1), 0.0, 0);
	uc_stub_run(6000); /* t = 9 s */
	UC_CHECK_NEAR(uc_stub_obj_pv(BO, 1), 0.0, 0);
	uc_stub_run(1000); /* t = 10 s, next cycle */
	UC_CHECK_NEAR(uc_stub_obj_pv(BO, 1), 1.0, 0);
	/* written on changes only (plus the refresh) */
	UC_CHECK(uc_stub_obj_writes(BO, 1) <= 4);
}

static void test_remote_output_priority_and_stop(void)
{
	uc_stub_param_set("out_device", "2002");
	uc_stub_param_set("out_instance", "3");
	uc_stub_param_set("out_priority", "9");
	uc_stub_obj_add(AI, 1, "Room", 20.0);
	uc_stub_remote_add(2002, AO, 3, 0.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_remote_value(2002, AO, 3), 20.0, 1e-6);
	UC_CHECK_INT(uc_stub_remote_last_priority(2002, AO, 3), 9);
	uc_stub_stop();
	UC_CHECK(uc_stub_remote_relinquished(2002, AO, 3));
	UC_CHECK_INT(uc_stub_subs_active(), 0);
}

static void test_invalid_parameters_use_defaults(void)
{
	local_io(20.0);
	uc_stub_param_set("kp", "fast");
	uc_stub_param_set("setpoint", "99");
	uc_stub_param_set("action", "warm");
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_WRN, "param kp=\"fast\"", 1);
	UC_CHECK_LOG(UC_LOG_WRN, "param setpoint=\"99\"", 1);
	UC_CHECK_LOG(UC_LOG_WRN, "param action=\"warm\"", 1);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 20.0, 1e-4); /* kp 20, sp 21, heat */
}

static void test_setpoint_with_real_precision(void)
{
	/* 21.3 is not exact as a REAL: no spurious setpoint change */
	local_io(20.0);
	uc_stub_param_set("setpoint", "21.3");
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(5000);
	UC_CHECK_LOG(0, "setpoint", 0);
	UC_CHECK_INT(uc_stub_kv_get("setpoint", NULL, 0), -1);
	UC_CHECK_NEAR(uc_stub_obj_pv(AO, 1), 26.0, 0.2); /* P = 20 * 1.3 */
}

static void test_setpoint_object_taken(void)
{
	local_io(20.0);
	uc_stub_obj_add(AV, 1, "someone else", 0.0);
	UC_CHECK_INT(uc_stub_start(), UC_ERR_EXISTS);
	UC_CHECK_LOG(UC_LOG_ERR, "setpoint AV:1: create failed (-11)", 1);
	UC_CHECK(!uc_stub_running());
}

int main(void)
{
	UC_RUN(test_defaults_local_sensor);
	UC_RUN(test_remote_sensor_cov);
	UC_RUN(test_poll_fallback_when_subscribe_fails);
	UC_RUN(test_lost_notification_polled);
	UC_RUN(test_stale_sensor_failsafe);
	UC_RUN(test_setpoint_written_by_client);
	UC_RUN(test_setpoint_persisted_with_kv);
	UC_RUN(test_no_kv_permission);
	UC_RUN(test_anti_windup);
	UC_RUN(test_integral_limited);
	UC_RUN(test_cooling_action);
	UC_RUN(test_binary_output_time_proportioning);
	UC_RUN(test_remote_output_priority_and_stop);
	UC_RUN(test_invalid_parameters_use_defaults);
	UC_RUN(test_setpoint_with_real_precision);
	UC_RUN(test_setpoint_object_taken);
	return uc_test_summary();
}

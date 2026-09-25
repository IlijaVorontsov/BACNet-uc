/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host-stub tests of the alarm example: thresholds and hysteresis, delays,
 * the persistent trip counter (uc_kv_*), the counter object and stale
 * sources.
 */

#include "uc_test.h"

#define AI  UC_OBJ_ANALOG_INPUT
#define AV  UC_OBJ_ANALOG_VALUE
#define BV  UC_OBJ_BINARY_VALUE
#define PV  UC_PROP_PRESENT_VALUE
#define DEV 1001u

/* Trip counter and active flag from the stored state (-1: none). */
static long stored_trips(int *active)
{
	uint8_t b[16];

	if (uc_stub_kv_get("state", b, sizeof(b)) != 12 || memcmp(b, "ALM1", 4) != 0) {
		return -1;
	}
	if (active != NULL) {
		*active = b[8];
	}
	return (long)(b[4] | (b[5] << 8) | (b[6] << 16) | ((uint32_t)b[7] << 24));
}

static void set_temp(double v)
{
	uc_stub_obj_set_pv(AI, 1, v);
	uc_stub_run(0);
}

static void test_high_trip_and_hysteresis(void)
{
	int active = -1;

	uc_stub_obj_add(AI, 1, "Room", 25.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK(uc_stub_obj_app_owned(BV, 10));
	UC_CHECK(strcmp(uc_stub_obj_name(BV, 10), "alarm") == 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 0.0, 0);

	set_temp(30.0); /* not above the threshold */
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 0.0, 0);
	set_temp(30.5); /* trips on the notification, not on the next tick */
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 1.0, 0);
	UC_CHECK_LOG(UC_LOG_WRN, "ALARM AI:1 = 30.50 > 30.00 (trip 1)", 1);
	UC_CHECK_INT(stored_trips(&active), 1);
	UC_CHECK_INT(active, 1);

	set_temp(29.2); /* inside the hysteresis band */
	uc_stub_run(5000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 1.0, 0);
	set_temp(28.9);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 0.0, 0);
	UC_CHECK_LOG(UC_LOG_INF, "cleared AI:1 = 28.90", 1);
	UC_CHECK_INT(stored_trips(&active), 1);
	UC_CHECK_INT(active, 0);

	set_temp(31.0);
	UC_CHECK_INT(stored_trips(NULL), 2);
}

static void test_low_direction(void)
{
	uc_stub_obj_add(AI, 2, "Supply", 8.0);
	uc_stub_param_set("src_instance", "2");
	uc_stub_param_set("direction", "low");
	uc_stub_param_set("threshold", "5");
	uc_stub_param_set("hysteresis", "0.5");
	uc_stub_param_set("bv_instance", "3");
	uc_stub_param_set("name", "frost");
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK(strcmp(uc_stub_obj_name(BV, 3), "frost") == 0);
	uc_stub_obj_set_pv(AI, 2, 4.9);
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 3), 1.0, 0);
	uc_stub_obj_set_pv(AI, 2, 5.4);
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 3), 1.0, 0);
	uc_stub_obj_set_pv(AI, 2, 5.6);
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 3), 0.0, 0);
}

static void test_on_and_off_delay(void)
{
	uc_stub_obj_add(AI, 1, "Room", 25.0);
	uc_stub_param_set("delay_ms", "5000");
	uc_stub_param_set("clear_delay_ms", "2000");
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(1000);

	set_temp(35.0);
	uc_stub_run(3000);
	set_temp(25.0); /* back before the delay elapsed: no trip */
	uc_stub_run(10000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 0.0, 0);
	UC_CHECK_INT(stored_trips(NULL), -1);

	set_temp(35.0);
	uc_stub_run(4000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 0.0, 0);
	uc_stub_run(1000); /* 5 s: evaluated on the tick */
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 1.0, 0);

	set_temp(20.0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 1.0, 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 0.0, 0);
	UC_CHECK_INT(stored_trips(NULL), 1);
}

static void test_state_survives_restart(void)
{
	uc_stub_obj_add(AI, 1, "Room", 25.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	set_temp(32.0);
	UC_CHECK_INT(stored_trips(NULL), 1);
	uc_stub_stop();
	UC_CHECK(!uc_stub_obj_exists(BV, 10));

	/* restart while the condition persists: active at once, not recounted */
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 1.0, 0);
	UC_CHECK_LOG(UC_LOG_INF, "-> BV:10 (active, 1 trips)", 1);
	uc_stub_run(5000);
	UC_CHECK_INT(stored_trips(NULL), 1);

	set_temp(20.0);
	set_temp(40.0);
	UC_CHECK_INT(stored_trips(NULL), 2);
	UC_CHECK_LOG(UC_LOG_WRN, "(trip 2)", 1);
}

static void test_counter_object_and_reset(void)
{
	double v;

	uc_stub_obj_add(AI, 1, "Room", 25.0);
	uc_stub_param_set("count_instance", "11");
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK(uc_stub_obj_app_owned(AV, 11));
	set_temp(31.0);
	set_temp(20.0);
	set_temp(31.0);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 11), 2.0, 0);

	/* other values do not change the counter */
	uc_stub_client_write(AV, 11, PV, 7.0, 8);
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 11), 2.0, 0);
	UC_CHECK(!uc_stub_obj_prio(AV, 11, 8, &v));

	/* writing 0 resets it, the priority slot is cleared again */
	uc_stub_client_write(AV, 11, PV, 0.0, 8);
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 11), 0.0, 0);
	UC_CHECK(!uc_stub_obj_prio(AV, 11, 8, &v));
	UC_CHECK_INT(stored_trips(NULL), 0);
	UC_CHECK_LOG(UC_LOG_INF, "trip counter reset (was 2)", 1);
}

static void test_without_kv_permission(void)
{
	uc_stub_set_perms(UC_STUB_PERM_LOCAL);
	uc_stub_obj_add(AI, 1, "Room", 25.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_INF, "not persistent", 1);
	set_temp(31.0);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 1.0, 0);
	UC_CHECK_INT(stored_trips(NULL), -1);
	uc_stub_stop();
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_INF, "-> BV:10 (normal, 0 trips)", 2); /* RAM only */
}

static void test_corrupt_state_ignored(void)
{
	uc_stub_kv_set("state", "garbage", 7);
	uc_stub_obj_add(AI, 1, "Room", 25.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_WRN, "stored state ignored (7)", 1);
	set_temp(31.0);
	UC_CHECK_INT(stored_trips(NULL), 1);
}

static void test_kv_write_failure_logged(void)
{
	uc_stub_obj_add(AI, 1, "Room", 25.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_fail(UC_STUB_FN_KV_SET, UC_ERR_IO, 1);
	set_temp(31.0);
	UC_CHECK_LOG(UC_LOG_WRN, "state not saved (-9)", 1);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 1.0, 0);
}

static void test_remote_source_polled_and_stale(void)
{
	uc_stub_param_set("src_device", "1001");
	uc_stub_param_set("poll_ms", "1000");
	uc_stub_remote_add(DEV, AI, 1, 20.0);
	uc_stub_fail(UC_STUB_FN_COV_SUBSCRIBE, UC_ERR_NO_MEM, -1);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_WRN, "COV subscription failed (-6), polling every 1000 ms", 1);

	uc_stub_remote_set(DEV, AI, 1, 33.0, false);
	uc_stub_run(2000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 1.0, 0);

	/* source unreachable: state kept, one warning */
	uc_stub_remote_fail(DEV, AI, 1, UC_ERR_NO_ROUTE);
	uc_stub_run(120000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 1.0, 0);
	UC_CHECK_LOG(UC_LOG_WRN, "no value for 60000 ms (err -12), state kept", 1);
	UC_CHECK_LOG(UC_LOG_WRN, "read failed (-12)", 1);

	uc_stub_remote_fail(DEV, AI, 1, 0);
	uc_stub_remote_set(DEV, AI, 1, 10.0, false);
	uc_stub_run(5000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 10), 0.0, 0);
	UC_CHECK_LOG(UC_LOG_INF, "value 10.00 again", 1);
	UC_CHECK_INT(uc_stub_log_excess(), 0);
	uc_stub_fail(UC_STUB_FN_COV_SUBSCRIBE, 0, 0);
}

static void test_bv_taken(void)
{
	uc_stub_obj_add(BV, 10, "io", 0.0);
	UC_CHECK_INT(uc_stub_start(), UC_ERR_EXISTS);
	UC_CHECK_LOG(UC_LOG_ERR, "BV:10: create failed (-11)", 1);
}

int main(void)
{
	UC_RUN(test_high_trip_and_hysteresis);
	UC_RUN(test_low_direction);
	UC_RUN(test_on_and_off_delay);
	UC_RUN(test_state_survives_restart);
	UC_RUN(test_counter_object_and_reset);
	UC_RUN(test_without_kv_permission);
	UC_RUN(test_corrupt_state_ignored);
	UC_RUN(test_kv_write_failure_logged);
	UC_RUN(test_remote_source_polled_and_stale);
	UC_RUN(test_bv_taken);
	return uc_test_summary();
}

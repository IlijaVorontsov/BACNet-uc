/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host-stub tests of uc-link (README.md in this directory): parameter
 * parsing including malformed input, the cov and poll paths, local
 * sources, destinations and the COV fallback.
 */

#include "uc_test.h"

#define AI  UC_OBJ_ANALOG_INPUT
#define AO  UC_OBJ_ANALOG_OUTPUT
#define AV  UC_OBJ_ANALOG_VALUE
#define BI  UC_OBJ_BINARY_INPUT
#define BO  UC_OBJ_BINARY_OUTPUT
#define BV  UC_OBJ_BINARY_VALUE
#define MSV UC_OBJ_MULTI_STATE_VALUE
#define PV  UC_PROP_PRESENT_VALUE
#define DEV 1001u

static void links(const char *count, const char *const *values)
{
	char key[4] = "l0";

	uc_stub_param_set("count", count);
	for (int i = 0; values[i] != NULL; i++) {
		key[1] = (char)('0' + i);
		uc_stub_param_set(key, values[i]);
	}
}

static void test_readme_example_cov(void)
{
	const char *const l[] = {"1001 0 1 2 10 cov 1000 0 1 0", NULL};

	uc_stub_remote_add(DEV, AI, 1, 20.5);
	links("1", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK(uc_stub_obj_app_owned(AV, 10));
	UC_CHECK(strcmp(uc_stub_obj_name(AV, 10), "link-0") == 0);
	UC_CHECK(uc_stub_sub_find(DEV, AI, 1) >= 0);
	UC_CHECK_LOG(UC_LOG_INF, "1 of 1 links active, tick 1000 ms", 1);

	/* initial notification right after subscribing */
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 10), 20.5, 1e-6);
	/* every notification is written */
	uc_stub_remote_set(DEV, AI, 1, 21.25, true);
	uc_stub_run(10);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 10), 21.25, 1e-6);
	uc_stub_run(60000);
	UC_CHECK_INT(uc_stub_obj_writes(AV, 10), 2);
	UC_CHECK_INT(uc_stub_remote_reads(DEV, AI, 1), 0); /* no polling */
}

static void test_scale_offset_priority(void)
{
	const char *const l[] = {"1001 0 1 1 4 cov 1000 8 -0.5 1e2", NULL};
	double v;

	uc_stub_obj_add(AO, 4, "Damper", 0.0);
	uc_stub_remote_add(DEV, AI, 1, 40.0);
	links("1", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK(!uc_stub_obj_app_owned(AO, 4));
	uc_stub_run(0);
	UC_CHECK(uc_stub_obj_prio(AO, 4, 8, &v));
	UC_CHECK_NEAR(v, 80.0, 1e-6); /* 40 * -0.5 + 100 */

	/* stop: the priority written into a foreign object is relinquished */
	uc_stub_stop();
	UC_CHECK(!uc_stub_obj_prio(AO, 4, 8, NULL));
	UC_CHECK(uc_stub_obj_exists(AO, 4));
}

static void test_poll_path_writes_on_change(void)
{
	const char *const l[] = {"1001 0 7 2 20 poll 500 0 2 1", NULL};

	uc_stub_remote_add(DEV, AI, 7, 3.0);
	links("1", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_INT(uc_stub_subs_active(), 0);
	UC_CHECK_INT(uc_stub_tick_period(), 500);

	uc_stub_run(500);
	UC_CHECK_INT(uc_stub_remote_reads(DEV, AI, 7), 1);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 20), 7.0, 1e-6);
	uc_stub_run(5000);
	UC_CHECK_INT(uc_stub_remote_reads(DEV, AI, 7), 11);
	UC_CHECK_INT(uc_stub_obj_writes(AV, 20), 1); /* unchanged: no rewrite */

	uc_stub_remote_set(DEV, AI, 7, 4.0, false);
	uc_stub_run(500);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 20), 9.0, 1e-6);
	UC_CHECK_INT(uc_stub_obj_writes(AV, 20), 2);
}

static void test_poll_read_errors_logged_once(void)
{
	const char *const l[] = {"1001 3 2 5 1 poll 1000 0 1 0", NULL};

	uc_stub_remote_add(DEV, BI, 2, 1.0);
	links("1", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 1), 1.0, 0);

	uc_stub_remote_fail(DEV, BI, 2, UC_ERR_TIMEOUT);
	uc_stub_run(30000);
	UC_CHECK_LOG(UC_LOG_WRN, "l0: read 1001 BI:2 failed (-4)", 1);
	uc_stub_remote_fail(DEV, BI, 2, 0);
	uc_stub_remote_set(DEV, BI, 2, 0.0, false);
	uc_stub_run(3000);
	UC_CHECK_LOG(UC_LOG_INF, "l0: read ok again", 1);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 1), 0.0, 0);
	UC_CHECK_INT(uc_stub_log_excess(), 0);
}

static void test_local_sources(void)
{
	/* local instance (1000 in the stub) with cov; UC_DEVICE_LOCAL alias
	 * with poll */
	const char *const l[] = {"1000 0 3 2 30 cov 1000 0 1 0",
				 "4294967295 0 3 2 31 poll 200 0 1 0.5", NULL};

	uc_stub_obj_add(AI, 3, "Supply", 12.0);
	links("2", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK(uc_stub_sub_find(UC_DEVICE_LOCAL, AI, 3) >= 0);
	UC_CHECK_INT(uc_stub_tick_period(), 200);
	uc_stub_run(200);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 30), 12.0, 1e-6);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 31), 12.5, 1e-6);

	uc_stub_obj_set_pv(AI, 3, 13.0); /* sampled input changes */
	uc_stub_run(200);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 30), 13.0, 1e-6);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 31), 13.5, 1e-6);
}

static void test_malformed_links_skipped(void)
{
	const char *const l[] = {
		"1001 0 1 2 10 cov 1000 0 1 0",     /* ok */
		"1001 0 1 2 11 cov 1000 0 1",       /* 9 fields */
		"1001 0 1 2 12 bogus 1000 0 1 0",   /* mode */
		"1001 0 1 2 13 poll 50 0 1 0",      /* period below 100 */
		"1001 0 1 2 14 poll 1000 17 1 0",   /* priority */
		"1001 0 1 2 15 poll 1000 0 1.5x 0", /* scale */
		"1001 0 1 2 16 poll 1000 0 1 0 9",  /* 11 fields */
		"4194303 0 1 2 17 poll 1000 0 1 0", /* device range */
		NULL,
	};

	uc_stub_remote_add(DEV, AI, 1, 1.0);
	links("8", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_ERR, "malformed", 7);
	UC_CHECK_LOG(UC_LOG_ERR, "l1: malformed (offset)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l2: malformed (mode)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l3: malformed (period_ms)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l4: malformed (priority)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l5: malformed (scale)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l6: malformed (trailing fields)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l7: malformed (src_device)", 1);
	UC_CHECK_LOG(UC_LOG_INF, "1 of 8 links active", 1);
	for (uint32_t i = 11; i <= 17; i++) {
		UC_CHECK(!uc_stub_obj_exists(AV, i));
	}
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 10), 1.0, 1e-6);
}

static void test_malformed_numbers(void)
{
	const char *const l[] = {
		"1001 -1 1 2 10 poll 1000 0 1 0",      /* negative type */
		"1001 1024 1 2 10 poll 1000 0 1 0",    /* type range */
		"1001 0 4194303 2 10 poll 1000 0 1 0", /* instance range */
		"1001 0 1 2 10 poll 1000 0 inf 0",     /* not finite */
		"1001 0 1 2 10 poll 1000 0 1 1e999",   /* overflow */
		"1001 0 1 2 10 poll 3600001 0 1 0",    /* period range */
		"1001 0x10 1 2 10 poll 1000 0 1 0",    /* hex */
		"",                                    /* empty */
		NULL,
	};

	links("8", l);
	UC_CHECK_INT(uc_stub_start(), UC_ERR_INVALID);
	UC_CHECK_LOG(UC_LOG_ERR, "l0: malformed (src_type)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l1: malformed (src_type)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l2: malformed (src_instance)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l3: malformed (scale)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l4: malformed (offset)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l5: malformed (period_ms)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l6: malformed (src_type)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l7: malformed (src_device)", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "no usable link", 1);
	UC_CHECK(!uc_stub_running());
}

static void test_blank_tolerance_and_cov_period_zero(void)
{
	const char *const l[] = {"  1001\t0 1  2 10 cov 0 0 +1.0 -0.25 ", NULL};

	uc_stub_remote_add(DEV, AI, 1, 2.0);
	links("1", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_INT(uc_stub_tick_period(), 1000);
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 10), 1.75, 1e-6);
}

static void test_count_invalid(void)
{
	static const char *const counts[] = {"0", "9", "abc", "-1", "", NULL};
	const char *const l[] = {"1001 0 1 2 10 cov 1000 0 1 0", NULL};

	/* missing */
	uc_stub_param_set("l0", l[0]);
	UC_CHECK_INT(uc_stub_start(), UC_ERR_INVALID);
	UC_CHECK_LOG(UC_LOG_ERR, "param count missing or not 1..8", 1);

	for (int i = 0; counts[i] != NULL; i++) {
		uc_stub_log_clear();
		links(counts[i], l);
		UC_CHECK_INT(uc_stub_start(), UC_ERR_INVALID);
		UC_CHECK_LOG(UC_LOG_ERR, "param count missing or not 1..8", 1);
	}
}

static void test_missing_link_param(void)
{
	const char *const l[] = {"1001 0 1 2 10 cov 1000 0 1 0", NULL};

	uc_stub_remote_add(DEV, AI, 1, 2.0);
	links("3", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_ERR, "l1: missing, skipped", 1);
	UC_CHECK_LOG(UC_LOG_ERR, "l2: missing, skipped", 1);
	UC_CHECK_LOG(UC_LOG_INF, "1 of 3 links active", 1);
}

static void test_destinations(void)
{
	const char *const l[] = {
		"1001 3 1 4 5 cov 1000 0 1 0",  /* BO:5 missing: skipped */
		"1001 3 1 4 6 cov 1000 0 1 0",  /* BO:6 bound in io.json */
		"1001 3 1 5 7 cov 1000 0 1 0",  /* BV:7 created */
		"1001 0 1 19 8 cov 1000 0 1 1", /* MSV:8 created */
		"1001 0 1 2 9 cov 1000 0 1 0",  /* AV:9 exists, owned by io */
		NULL,
	};

	uc_stub_obj_add(BO, 6, "LED", 0.0);
	uc_stub_obj_add(AV, 9, "foreign", 0.0);
	uc_stub_remote_add(DEV, BI, 1, 1.0);
	uc_stub_remote_add(DEV, AI, 1, 2.0);
	links("5", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_ERR, "l0: destination BO:5 not usable (-2), skipped", 1);
	UC_CHECK(uc_stub_obj_app_owned(BV, 7));
	UC_CHECK(uc_stub_obj_app_owned(MSV, 8));
	UC_CHECK(!uc_stub_obj_app_owned(AV, 9));
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(BO, 6), 1.0, 0);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 7), 1.0, 0);
	UC_CHECK_NEAR(uc_stub_obj_pv(MSV, 8), 3.0, 0);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 9), 2.0, 1e-6);

	uc_stub_stop();
	UC_CHECK(!uc_stub_obj_exists(BV, 7));
	UC_CHECK(uc_stub_obj_exists(AV, 9));
}

static void test_write_errors_logged_once(void)
{
	const char *const l[] = {"1001 0 1 19 8 cov 1000 0 1 0", NULL};

	uc_stub_remote_add(DEV, AI, 1, 2.0);
	links("1", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(0);
	UC_CHECK_NEAR(uc_stub_obj_pv(MSV, 8), 2.0, 0);
	/* 0 is not a valid multi-state value: rejected by the host */
	for (int i = 0; i < 5; i++) {
		uc_stub_remote_set(DEV, AI, 1, 0.0 - i, true);
		uc_stub_run(1000);
	}
	UC_CHECK_LOG(UC_LOG_WRN, "l0: write MSV:8 failed (-1)", 1);
	uc_stub_remote_set(DEV, AI, 1, 4.0, true);
	uc_stub_run(1000);
	UC_CHECK_LOG(UC_LOG_INF, "l0: write ok again", 1);
	UC_CHECK_NEAR(uc_stub_obj_pv(MSV, 8), 4.0, 0);
}

static void test_cov_subscribe_failure_falls_back_to_poll(void)
{
	const char *const l[] = {"1001 0 1 2 10 cov 2000 0 1 0", NULL};

	uc_stub_remote_add(DEV, AI, 1, 5.0);
	uc_stub_fail(UC_STUB_FN_COV_SUBSCRIBE, UC_ERR_BUSY, 2);
	links("1", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_LOG(UC_LOG_WRN, "l0: COV subscription failed (-5), polling every 2000 ms", 1);
	UC_CHECK_INT(uc_stub_tick_period(), 2000);

	uc_stub_run(2000);
	UC_CHECK_INT(uc_stub_remote_reads(DEV, AI, 1), 1);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 10), 5.0, 1e-6);
	/* retry at 30 s fails (second injected error), at 60 s succeeds */
	uc_stub_run(40000);
	UC_CHECK_INT(uc_stub_subs_active(), 0);
	uc_stub_run(20000);
	UC_CHECK_INT(uc_stub_subs_active(), 1);
	UC_CHECK_LOG(UC_LOG_INF, "l0: COV subscription active", 1);
	uint32_t reads = uc_stub_remote_reads(DEV, AI, 1);

	uc_stub_remote_set(DEV, AI, 1, 6.0, true);
	uc_stub_run(10000);
	UC_CHECK_NEAR(uc_stub_obj_pv(AV, 10), 6.0, 1e-6);
	UC_CHECK_INT(uc_stub_remote_reads(DEV, AI, 1), reads); /* polling stopped */
}

static void test_stop_unsubscribes(void)
{
	const char *const l[] = {"1001 0 1 2 10 cov 1000 0 1 0", "1001 0 2 2 11 cov 1000 0 1 0",
				 NULL};

	uc_stub_remote_add(DEV, AI, 1, 5.0);
	uc_stub_remote_add(DEV, AI, 2, 5.0);
	links("2", l);
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_INT(uc_stub_subs_active(), 2);
	uc_stub_stop();
	UC_CHECK_INT(uc_stub_subs_active(), 0);
	UC_CHECK_LOG(UC_LOG_INF, "stopped", 1);
}

int main(void)
{
	UC_RUN(test_readme_example_cov);
	UC_RUN(test_scale_offset_priority);
	UC_RUN(test_poll_path_writes_on_change);
	UC_RUN(test_poll_read_errors_logged_once);
	UC_RUN(test_local_sources);
	UC_RUN(test_malformed_links_skipped);
	UC_RUN(test_malformed_numbers);
	UC_RUN(test_blank_tolerance_and_cov_period_zero);
	UC_RUN(test_count_invalid);
	UC_RUN(test_missing_link_param);
	UC_RUN(test_destinations);
	UC_RUN(test_write_errors_logged_once);
	UC_RUN(test_cov_subscribe_failure_falls_back_to_poll);
	UC_RUN(test_stop_unsubscribes);
	return uc_test_summary();
}

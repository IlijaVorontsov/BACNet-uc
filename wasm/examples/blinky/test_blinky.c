/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host-stub tests of the blinky example.
 */

#include "uc_test.h"

#define BO UC_OBJ_BINARY_OUTPUT
#define BV UC_OBJ_BINARY_VALUE

static void test_default_binary_value(void)
{
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK(uc_stub_obj_app_owned(BV, 1));
	UC_CHECK(strcmp(uc_stub_obj_name(BV, 1), "blinky") == 0);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 1), 0.0, 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 1), 1.0, 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 1), 0.0, 0);
	uc_stub_run(8000);
	UC_CHECK_INT(uc_stub_ticks(), 10);
	uc_stub_stop();
	UC_CHECK(!uc_stub_obj_exists(BV, 1));
	UC_CHECK_LOG(UC_LOG_INF, "stopped after 10 toggles", 1);
}

static void test_output_with_priority(void)
{
	double v;

	uc_stub_obj_add(BO, 1, "LED", 0.0);
	uc_stub_param_set("type", "4");
	uc_stub_param_set("priority", "8");
	uc_stub_param_set("period_ms", "250");
	UC_CHECK_INT(uc_stub_start(), 0);
	UC_CHECK_INT(uc_stub_tick_period(), 250);
	uc_stub_run(250);
	UC_CHECK(uc_stub_obj_prio(BO, 1, 8, &v));
	UC_CHECK_NEAR(v, 1.0, 0);
	uc_stub_stop();
	UC_CHECK(!uc_stub_obj_prio(BO, 1, 8, NULL));
	UC_CHECK(uc_stub_obj_exists(BO, 1));
}

/* A value object has no priority array: the priority is ignored and a
 * regular stop writes "off" instead of relinquishing. */
static void test_value_object_with_priority(void)
{
	uc_stub_obj_add(BV, 3, "Flag", 0.0);
	uc_stub_param_set("instance", "3");
	uc_stub_param_set("priority", "8");
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(1000);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 3), 1.0, 0);
	UC_CHECK(!uc_stub_obj_prio(BV, 3, 8, NULL));
	UC_CHECK_INT(uc_stub_client_relinquish(BV, 3, 8), UC_OK);
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 3), 1.0, 0);
	uc_stub_stop();
	UC_CHECK(uc_stub_obj_exists(BV, 3));
	UC_CHECK_NEAR(uc_stub_obj_pv(BV, 3), 0.0, 0);
}

static void test_missing_output_fails(void)
{
	uc_stub_param_set("type", "4");
	uc_stub_param_set("instance", "2");
	UC_CHECK_INT(uc_stub_start(), UC_ERR_NOT_FOUND);
	UC_CHECK_LOG(UC_LOG_ERR, "BO:2 not available (-2)", 1);
}

static void test_raw_channel(void)
{
	uc_stub_io_add("do0", 0.0, true);
	uc_stub_param_set("channel", "do0");
	uc_stub_param_set("period_ms", "100");
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_run(100);
	UC_CHECK_NEAR(uc_stub_io_get("do0"), 1.0, 0);
	uc_stub_run(100);
	UC_CHECK_NEAR(uc_stub_io_get("do0"), 0.0, 0);
	uc_stub_run(100);
	uc_stub_stop();
	UC_CHECK_NEAR(uc_stub_io_get("do0"), 0.0, 0);
}

static void test_channel_without_permission(void)
{
	uc_stub_set_perms(UC_STUB_PERM_LOCAL);
	uc_stub_io_add("do0", 0.0, true);
	uc_stub_param_set("channel", "do0");
	UC_CHECK_INT(uc_stub_start(), UC_ERR_PERM);
	UC_CHECK_LOG(UC_LOG_ERR, "channel do0 not available (-3)", 1);
}

int main(void)
{
	UC_RUN(test_default_binary_value);
	UC_RUN(test_output_with_priority);
	UC_RUN(test_value_object_with_priority);
	UC_RUN(test_missing_output_fails);
	UC_RUN(test_raw_channel);
	UC_RUN(test_channel_without_permission);
	return uc_test_summary();
}

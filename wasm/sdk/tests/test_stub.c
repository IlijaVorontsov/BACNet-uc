/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host tests of the host stub itself: values, priorities and raw IO must
 * behave like the firmware (uc_value_from_double(), the bacnet-stack
 * objects, uc_io_write()), so that an app that passes its stub tests does
 * not fail on the node.
 */

#include "uc_test.h"

UC_APP_DECLARE()

static void test_values_are_converted_not_truncated(void)
{
	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_BINARY_VALUE, 1, "bv", 0.0), 0);
	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_MULTI_STATE_VALUE, 1, "msv", 1.0), 0);
	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_ANALOG_VALUE, 1, "av", 0.0), 0);
	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_BINARY_OUTPUT, 1, "bo", 0.0), 0);
	UC_CHECK_INT(uc_stub_start(), 0);

	UC_CHECK_INT(uc_pv_write(UC_OBJ_BINARY_VALUE, 1, 0.5, 0), UC_ERR_INVALID);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_BINARY_VALUE, 1, 2.0, 0), UC_ERR_INVALID);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_BINARY_OUTPUT, 1, -1.0, 8), UC_ERR_INVALID);
	UC_CHECK_NEAR(uc_stub_obj_pv(UC_OBJ_BINARY_VALUE, 1), 0.0, 0);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_BINARY_VALUE, 1, 1.0, 0), UC_OK);
	UC_CHECK_NEAR(uc_stub_obj_pv(UC_OBJ_BINARY_VALUE, 1), 1.0, 0);

	UC_CHECK_INT(uc_pv_write(UC_OBJ_MULTI_STATE_VALUE, 1, 2.7, 0), UC_ERR_INVALID);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_MULTI_STATE_VALUE, 1, 0.0, 0), UC_ERR_INVALID);
	UC_CHECK_NEAR(uc_stub_obj_pv(UC_OBJ_MULTI_STATE_VALUE, 1), 1.0, 0);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_MULTI_STATE_VALUE, 1, 3.0, 0), UC_OK);
	UC_CHECK_NEAR(uc_stub_obj_pv(UC_OBJ_MULTI_STATE_VALUE, 1), 3.0, 0);

	UC_CHECK_INT(uc_pv_write(UC_OBJ_ANALOG_VALUE, 1, NAN, 0), UC_ERR_INVALID);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_ANALOG_VALUE, 1, INFINITY, 0), UC_ERR_INVALID);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_ANALOG_VALUE, 1, 1e39, 0), UC_ERR_INVALID);
	UC_CHECK_NEAR(uc_stub_obj_pv(UC_OBJ_ANALOG_VALUE, 1), 0.0, 0);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_ANALOG_VALUE, 1, 2.5, 0), UC_OK);
	UC_CHECK_NEAR(uc_stub_obj_pv(UC_OBJ_ANALOG_VALUE, 1), 2.5, 0);

	/* non-PV properties by their datatype */
	UC_CHECK_INT(uc_prop_write(UC_OBJ_MULTI_STATE_VALUE, 1, UC_PROP_NUMBER_OF_STATES,
				   UC_ARRAY_ALL, 3.5, 0),
		     UC_ERR_INVALID);
	UC_CHECK_INT(uc_prop_write(UC_OBJ_MULTI_STATE_VALUE, 1, UC_PROP_NUMBER_OF_STATES,
				   UC_ARRAY_ALL, 4.0, 0),
		     UC_OK);
	UC_CHECK_INT(uc_prop_write(UC_OBJ_ANALOG_VALUE, 1, UC_PROP_UNITS, UC_ARRAY_ALL, -1.0, 0),
		     UC_ERR_INVALID);
	UC_CHECK_INT(uc_prop_write(UC_OBJ_ANALOG_VALUE, 1, UC_PROP_OUT_OF_SERVICE, UC_ARRAY_ALL,
				   0.5, 0),
		     UC_ERR_INVALID);
	UC_CHECK_INT(uc_prop_write(UC_OBJ_ANALOG_VALUE, 1, UC_PROP_COV_INCREMENT, UC_ARRAY_ALL,
				   NAN, 0),
		     UC_ERR_INVALID);
	UC_CHECK_INT(uc_prop_write(UC_OBJ_ANALOG_VALUE, 1, UC_PROP_COV_INCREMENT, UC_ARRAY_ALL,
				   0.25, 0),
		     UC_OK);
	UC_CHECK_NEAR(uc_stub_obj_prop(UC_OBJ_ANALOG_VALUE, 1, UC_PROP_COV_INCREMENT), 0.25, 0);

	/* a BACnet client write goes through the same conversion */
	UC_CHECK_INT(uc_stub_client_write(UC_OBJ_BINARY_VALUE, 1, UC_PROP_PRESENT_VALUE, 0.5, 0),
		     UC_ERR_INVALID);
	/* the failed writes were counted like on the node */
	UC_CHECK(uc_stub_errors() >= 12);
}

static void test_priority_6(void)
{
	double v;

	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_ANALOG_VALUE, 1, "av", 0.0), 0);
	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_BINARY_VALUE, 1, "bv", 0.0), 0);
	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_MULTI_STATE_VALUE, 1, "msv", 1.0), 0);
	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_ANALOG_OUTPUT, 1, "ao", 0.0), 0);
	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_BINARY_OUTPUT, 1, "bo", 0.0), 0);
	UC_CHECK_INT(uc_stub_obj_add(UC_OBJ_MULTI_STATE_OUTPUT, 1, "mso", 1.0), 0);
	UC_CHECK_INT(uc_stub_start(), 0);

	/* analog-value: denied (write-access-denied); a relinquish succeeds */
	UC_CHECK_INT(uc_pv_write(UC_OBJ_ANALOG_VALUE, 1, 12.5, 6), UC_ERR_PERM);
	UC_CHECK_NEAR(uc_stub_obj_pv(UC_OBJ_ANALOG_VALUE, 1), 0.0, 0);
	UC_CHECK_INT(uc_prop_write_null(UC_OBJ_ANALOG_VALUE, 1, UC_PROP_PRESENT_VALUE, 6), UC_OK);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_ANALOG_VALUE, 1, 12.5, 8), UC_OK);
	/* binary-value and multi-state-value of the firmware ignore it */
	UC_CHECK_INT(uc_pv_write(UC_OBJ_BINARY_VALUE, 1, 1.0, 6), UC_OK);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_MULTI_STATE_VALUE, 1, 2.0, 6), UC_OK);
	/* outputs: write and relinquish denied */
	UC_CHECK_INT(uc_pv_write(UC_OBJ_ANALOG_OUTPUT, 1, 40.0, 6), UC_ERR_PERM);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_BINARY_OUTPUT, 1, 1.0, 6), UC_ERR_PERM);
	UC_CHECK_INT(uc_pv_write(UC_OBJ_MULTI_STATE_OUTPUT, 1, 2.0, 6), UC_ERR_PERM);
	UC_CHECK_INT(uc_prop_write_null(UC_OBJ_ANALOG_OUTPUT, 1, UC_PROP_PRESENT_VALUE, 6),
		     UC_ERR_PERM);
	UC_CHECK(!uc_stub_obj_prio(UC_OBJ_ANALOG_OUTPUT, 1, 6, &v));
	UC_CHECK_INT(uc_pv_write(UC_OBJ_ANALOG_OUTPUT, 1, 40.0, 8), UC_OK);
	UC_CHECK(uc_stub_obj_prio(UC_OBJ_ANALOG_OUTPUT, 1, 8, &v) && (v == 40.0));
	UC_CHECK_INT(uc_stub_client_write(UC_OBJ_ANALOG_VALUE, 1, UC_PROP_PRESENT_VALUE, 1.0, 6),
		     UC_ERR_PERM);
}

static void test_io_write_like_the_firmware(void)
{
	int32_t di, d_o, ao;

	UC_CHECK_INT(uc_stub_io_add("di0", 0.0, false), 0);
	UC_CHECK_INT(uc_stub_io_add("do0", 0.0, true), 0);
	UC_CHECK_INT(uc_stub_io_add("ao0", 0.0, true), 0);
	UC_CHECK_INT(uc_stub_start(), 0);
	di = uc_io_find("di0", 3);
	d_o = uc_io_find("do0", 3);
	ao = uc_io_find("ao0", 3);
	UC_CHECK(di >= 0 && d_o >= 0 && ao >= 0);

	UC_CHECK_INT(uc_io_write(di, 1.0), UC_ERR_PERM);
	UC_CHECK_INT(uc_io_write(d_o, NAN), UC_ERR_INVALID);
	UC_CHECK_INT(uc_io_write(ao, INFINITY), UC_ERR_INVALID);
	UC_CHECK_NEAR(uc_stub_io_get("do0"), 0.0, 0);
	UC_CHECK_INT(uc_io_write(d_o, 0.3), UC_OK);
	UC_CHECK_NEAR(uc_stub_io_get("do0"), 1.0, 0);
	UC_CHECK_INT(uc_io_write(ao, 150.0), UC_OK);
	UC_CHECK_NEAR(uc_stub_io_get("ao0"), 100.0, 0);
	UC_CHECK_INT(uc_io_write(ao, -5.0), UC_OK);
	UC_CHECK_NEAR(uc_stub_io_get("ao0"), 0.0, 0);
	UC_CHECK_INT(uc_io_write(ao, 42.5), UC_OK);
	UC_CHECK_NEAR(uc_stub_io_get("ao0"), 42.5, 0);
}

int main(void)
{
	UC_RUN(test_values_are_converted_not_truncated);
	UC_RUN(test_priority_6);
	UC_RUN(test_io_write_like_the_firmware);
	return uc_test_summary();
}

/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Unit tests of uc_common.c.
 */

#include <errno.h>
#include <math.h>
#include <string.h>

#include <zephyr/ztest.h>

#include "bacnet/bacenum.h"

#include "uc/uc_common.h"
#include "uc/uc_mgmt.h"

/* UC_ERR_* of the guest ABI */
#include "../../../../wasm/sdk/include/bacnet_uc.h"

ZTEST_SUITE(uc_common, NULL, NULL, NULL, NULL, NULL);

ZTEST(uc_common, test_perms)
{
	zassert_equal(uc_perm_from_str("bacnet.local"), UC_PERM_BACNET_LOCAL);
	zassert_equal(uc_perm_from_str("bacnet.remote"), UC_PERM_BACNET_REMOTE);
	zassert_equal(uc_perm_from_str("io"), UC_PERM_IO);
	zassert_equal(uc_perm_from_str("kv"), UC_PERM_KV);
	zassert_equal(uc_perm_from_str("net"), 0);
	zassert_equal(uc_perm_from_str("IO"), 0);
	zassert_equal(uc_perm_from_str(NULL), 0);

	zassert_str_equal(uc_perm_to_str(UC_PERM_IO), "io");
	zassert_str_equal(uc_perm_to_str(UC_PERM_BACNET_REMOTE), "bacnet.remote");
	zassert_is_null(uc_perm_to_str(UC_PERM_IO | UC_PERM_KV));
	zassert_is_null(uc_perm_to_str(0));
}

/* Table of uc_common.h */
static const struct {
	int err;
	int api;
	int mgmt;
} err_table[] = {
	{-EINVAL, UC_ERR_INVALID, UC_MGMT_RC_INVALID},
	{-ENOENT, UC_ERR_NOT_FOUND, UC_MGMT_RC_NOT_FOUND},
	{-EACCES, UC_ERR_PERM, UC_MGMT_RC_PERM},
	{-ETIMEDOUT, UC_ERR_TIMEOUT, UC_MGMT_RC_BUSY},
	{-ECANCELED, UC_ERR_TIMEOUT, UC_MGMT_RC_BUSY},
	{-EBUSY, UC_ERR_BUSY, UC_MGMT_RC_BUSY},
	{-EAGAIN, UC_ERR_BUSY, UC_MGMT_RC_BUSY},
	{-ENOMEM, UC_ERR_NO_MEM, UC_MGMT_RC_NO_MEM},
	{-EREMOTEIO, UC_ERR_BACNET, UC_MGMT_RC_UNKNOWN},
	{-ENOTSUP, UC_ERR_UNSUPPORTED, UC_MGMT_RC_UNSUPPORTED},
	{-EIO, UC_ERR_IO, UC_MGMT_RC_IO},
	{-EBADMSG, UC_ERR_TYPE, UC_MGMT_RC_INVALID},
	{-EEXIST, UC_ERR_EXISTS, UC_MGMT_RC_EXISTS},
	{-EHOSTUNREACH, UC_ERR_NO_ROUTE, UC_MGMT_RC_NOT_FOUND},
	{-EALREADY, UC_ERR_INVALID, UC_MGMT_RC_STATE},
	{-ESRCH, UC_ERR_INVALID, UC_MGMT_RC_STATE},
	{-EILSEQ, UC_ERR_INVALID, UC_MGMT_RC_VERIFY},
	{-ENOSPC, UC_ERR_NO_MEM, UC_MGMT_RC_LIMIT},
};

ZTEST(uc_common, test_error_maps)
{
	for (size_t i = 0; i < ARRAY_SIZE(err_table); i++) {
		zassert_equal(uc_err_to_api(err_table[i].err), err_table[i].api,
			      "api map of %d", err_table[i].err);
		zassert_equal(uc_err_to_mgmt(err_table[i].err), err_table[i].mgmt,
			      "mgmt map of %d", err_table[i].err);
		zassert_not_null(uc_err_str(err_table[i].err));
	}

	/* success values pass through the API map, map to OK for mgmt */
	zassert_equal(uc_err_to_api(0), 0);
	zassert_equal(uc_err_to_api(7), 7);
	zassert_equal(uc_err_to_mgmt(0), UC_MGMT_RC_OK);
	zassert_equal(uc_err_to_mgmt(3), UC_MGMT_RC_OK);
	zassert_str_equal(uc_err_str(0), "ok");
}

ZTEST(uc_common, test_obj_type_text)
{
	uint16_t t;

	zassert_ok(uc_obj_type_from_str("analog-input", &t));
	zassert_equal(t, OBJECT_ANALOG_INPUT);
	zassert_ok(uc_obj_type_from_str("multi-state-value", &t));
	zassert_equal(t, OBJECT_MULTI_STATE_VALUE);
	zassert_ok(uc_obj_type_from_str("binary-output", &t));
	zassert_equal(t, OBJECT_BINARY_OUTPUT);
	zassert_ok(uc_obj_type_from_str("8", &t));
	zassert_equal(t, OBJECT_DEVICE);
	zassert_ok(uc_obj_type_from_str("1023", &t));
	zassert_equal(t, 1023);

	zassert_equal(uc_obj_type_from_str("1024", &t), -EINVAL);
	zassert_equal(uc_obj_type_from_str("analog-foo", &t), -EINVAL);
	zassert_equal(uc_obj_type_from_str("", &t), -EINVAL);
	zassert_equal(uc_obj_type_from_str("12x", &t), -EINVAL);
	zassert_equal(uc_obj_type_from_str(NULL, &t), -EINVAL);

	zassert_str_equal(uc_obj_type_to_str(OBJECT_ANALOG_VALUE), "analog-value");
	zassert_str_equal(uc_obj_type_to_str(OBJECT_MULTI_STATE_INPUT), "multi-state-input");
}

ZTEST(uc_common, test_prop_and_units_text)
{
	uint32_t p;
	uint16_t u;

	zassert_ok(uc_prop_from_str("present-value", &p));
	zassert_equal(p, PROP_PRESENT_VALUE);
	zassert_ok(uc_prop_from_str("object-name", &p));
	zassert_equal(p, PROP_OBJECT_NAME);
	zassert_ok(uc_prop_from_str("85", &p));
	zassert_equal(p, 85);
	zassert_equal(uc_prop_from_str("present-valu", &p), -EINVAL);
	zassert_equal(uc_prop_from_str("-1", &p), -EINVAL);

	zassert_ok(uc_units_from_str("degrees-celsius", &u));
	zassert_equal(u, UNITS_DEGREES_CELSIUS);
	zassert_ok(uc_units_from_str("percent", &u));
	zassert_equal(u, UNITS_PERCENT);
	zassert_ok(uc_units_from_str("no-units", &u));
	zassert_equal(u, UNITS_NO_UNITS);
	zassert_ok(uc_units_from_str("volts", &u));
	zassert_equal(u, UNITS_VOLTS);
	zassert_ok(uc_units_from_str("62", &u));
	zassert_equal(u, UNITS_DEGREES_CELSIUS);
	zassert_equal(uc_units_from_str("degrees-kelvinish", &u), -EINVAL);
	zassert_equal(uc_units_from_str("65536", &u), -EINVAL);
}

ZTEST(uc_common, test_obj_type_classes)
{
	static const uint16_t supported[] = {
		OBJECT_ANALOG_INPUT,       OBJECT_ANALOG_OUTPUT,       OBJECT_ANALOG_VALUE,
		OBJECT_BINARY_INPUT,       OBJECT_BINARY_OUTPUT,       OBJECT_BINARY_VALUE,
		OBJECT_MULTI_STATE_INPUT,  OBJECT_MULTI_STATE_OUTPUT,  OBJECT_MULTI_STATE_VALUE,
	};

	for (size_t i = 0; i < ARRAY_SIZE(supported); i++) {
		zassert_true(uc_obj_type_supported(supported[i]));
	}
	zassert_false(uc_obj_type_supported(OBJECT_DEVICE));
	zassert_false(uc_obj_type_supported(OBJECT_NETWORK_PORT));

	zassert_true(uc_obj_type_is_analog(OBJECT_ANALOG_VALUE));
	zassert_false(uc_obj_type_is_analog(OBJECT_BINARY_VALUE));
	zassert_true(uc_obj_type_is_binary(OBJECT_BINARY_INPUT));
	zassert_false(uc_obj_type_is_binary(OBJECT_MULTI_STATE_INPUT));
	zassert_true(uc_obj_type_is_multistate(OBJECT_MULTI_STATE_OUTPUT));
	zassert_false(uc_obj_type_is_multistate(OBJECT_ANALOG_OUTPUT));
}

ZTEST(uc_common, test_value_to_double)
{
	BACNET_APPLICATION_DATA_VALUE v = {0};
	double d;

	v.tag = BACNET_APPLICATION_TAG_REAL;
	v.type.Real = 21.5f;
	zassert_ok(uc_value_to_double(&v, &d));
	zassert_within(d, 21.5, 1e-6);

	v.tag = BACNET_APPLICATION_TAG_UNSIGNED_INT;
	v.type.Unsigned_Int = 4000000000U;
	zassert_ok(uc_value_to_double(&v, &d));
	zassert_within(d, 4000000000.0, 0.5);

	v.tag = BACNET_APPLICATION_TAG_SIGNED_INT;
	v.type.Signed_Int = -12;
	zassert_ok(uc_value_to_double(&v, &d));
	zassert_within(d, -12.0, 1e-9);

	v.tag = BACNET_APPLICATION_TAG_ENUMERATED;
	v.type.Enumerated = BINARY_ACTIVE;
	zassert_ok(uc_value_to_double(&v, &d));
	zassert_within(d, 1.0, 1e-9);

	v.tag = BACNET_APPLICATION_TAG_BOOLEAN;
	v.type.Boolean = true;
	zassert_ok(uc_value_to_double(&v, &d));
	zassert_within(d, 1.0, 1e-9);

	v.tag = BACNET_APPLICATION_TAG_CHARACTER_STRING;
	zassert_equal(uc_value_to_double(&v, &d), -EBADMSG);
	v.tag = BACNET_APPLICATION_TAG_NULL;
	zassert_equal(uc_value_to_double(&v, &d), -EBADMSG);
}

ZTEST(uc_common, test_value_from_double)
{
	BACNET_APPLICATION_DATA_VALUE v;

	/* analog PV -> REAL */
	zassert_ok(uc_value_from_double(OBJECT_ANALOG_VALUE, PROP_PRESENT_VALUE, 21.25, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_REAL);
	zassert_within((double)v.type.Real, 21.25, 1e-6);
	zassert_ok(uc_value_from_double(OBJECT_ANALOG_OUTPUT, PROP_PRIORITY_ARRAY, -3.5, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_REAL);

	/* binary PV -> ENUMERATED 0/1 */
	zassert_ok(uc_value_from_double(OBJECT_BINARY_OUTPUT, PROP_PRESENT_VALUE, 1.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_ENUMERATED);
	zassert_equal(v.type.Enumerated, BINARY_ACTIVE);
	zassert_ok(uc_value_from_double(OBJECT_BINARY_VALUE, PROP_PRESENT_VALUE, 0.0, &v));
	zassert_equal(v.type.Enumerated, BINARY_INACTIVE);
	zassert_ok(uc_value_from_double(OBJECT_BINARY_VALUE, PROP_RELINQUISH_DEFAULT, 1.0, &v));
	zassert_equal(v.type.Enumerated, BINARY_ACTIVE);
	zassert_ok(uc_value_from_double(OBJECT_BINARY_OUTPUT, PROP_PRIORITY_ARRAY, 0.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_ENUMERATED);
	zassert_equal(v.type.Enumerated, BINARY_INACTIVE);

	/* multi-state PV -> UNSIGNED */
	zassert_ok(uc_value_from_double(OBJECT_MULTI_STATE_VALUE, PROP_PRESENT_VALUE, 3.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_UNSIGNED_INT);
	zassert_equal(v.type.Unsigned_Int, 3);

	/* other numeric properties */
	zassert_ok(uc_value_from_double(OBJECT_ANALOG_INPUT, PROP_OUT_OF_SERVICE, 1.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_BOOLEAN);
	zassert_true(v.type.Boolean);
	zassert_ok(uc_value_from_double(OBJECT_ANALOG_INPUT, PROP_OUT_OF_SERVICE, 0.0, &v));
	zassert_false(v.type.Boolean);
	zassert_ok(uc_value_from_double(OBJECT_ANALOG_INPUT, PROP_COV_INCREMENT, 0.5, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_REAL);
	zassert_ok(uc_value_from_double(OBJECT_ANALOG_INPUT, PROP_UNITS, 62.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_ENUMERATED);
	zassert_equal(v.type.Enumerated, 62);
	zassert_ok(uc_value_from_double(OBJECT_MULTI_STATE_INPUT, PROP_NUMBER_OF_STATES, 4.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_UNSIGNED_INT);
	zassert_ok(uc_value_from_double(OBJECT_DEVICE, PROP_UTC_OFFSET, -60.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_SIGNED_INT);
	zassert_equal(v.type.Signed_Int, -60);
	zassert_ok(uc_value_from_double(OBJECT_DEVICE, PROP_APDU_TIMEOUT, 4294967295.0, &v));
	zassert_equal(v.type.Unsigned_Int, 4294967295U);

	/* numeric standard objects of remote devices (uc_remote_write) */
	zassert_ok(uc_value_from_double(OBJECT_INTEGER_VALUE, PROP_PRESENT_VALUE, -7.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_SIGNED_INT);
	zassert_equal(v.type.Signed_Int, -7);
	zassert_ok(uc_value_from_double(OBJECT_INTEGER_VALUE, PROP_COV_INCREMENT, 2.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_UNSIGNED_INT);
	zassert_ok(uc_value_from_double(OBJECT_INTEGER_VALUE, PROP_HIGH_LIMIT, -2.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_SIGNED_INT);
	zassert_ok(uc_value_from_double(OBJECT_POSITIVE_INTEGER_VALUE, PROP_PRESENT_VALUE, 9.0,
					&v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_UNSIGNED_INT);
	zassert_equal(v.type.Unsigned_Int, 9);
	zassert_ok(uc_value_from_double(OBJECT_LARGE_ANALOG_VALUE, PROP_PRESENT_VALUE, 1e300, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_DOUBLE);
	zassert_equal(v.type.Double, 1e300);
	zassert_ok(uc_value_from_double(OBJECT_LARGE_ANALOG_VALUE, PROP_PRIORITY_ARRAY, 0.5, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_DOUBLE);
	zassert_ok(uc_value_from_double(OBJECT_LOOP, PROP_SETPOINT, 21.5, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_REAL);
	zassert_ok(uc_value_from_double(OBJECT_LOOP, PROP_PROPORTIONAL_CONSTANT, 0.8, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_REAL);
	zassert_ok(uc_value_from_double(OBJECT_LOOP, PROP_ACTION, 1.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_ENUMERATED);
	zassert_ok(uc_value_from_double(OBJECT_LIGHTING_OUTPUT, PROP_PRESENT_VALUE, 50.0, &v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_REAL);
	zassert_ok(uc_value_from_double(OBJECT_BINARY_LIGHTING_OUTPUT, PROP_PRESENT_VALUE, 2.0,
					&v));
	zassert_equal(v.tag, BACNET_APPLICATION_TAG_ENUMERATED);
	zassert_equal(v.type.Enumerated, 2);
	/* range checks of those datatypes */
	zassert_equal(uc_value_from_double(OBJECT_INTEGER_VALUE, PROP_PRESENT_VALUE, 0.5, &v),
		      -EINVAL);
	zassert_equal(uc_value_from_double(OBJECT_POSITIVE_INTEGER_VALUE, PROP_PRESENT_VALUE,
					   -1.0, &v),
		      -EINVAL);

	/* not numeric (checked before the value) */
	zassert_equal(uc_value_from_double(OBJECT_COMMAND, PROP_ACTION, 1.0, &v), -EBADMSG);
	zassert_equal(uc_value_from_double(OBJECT_ANALOG_INPUT, PROP_OBJECT_NAME, 1.0, &v),
		      -EBADMSG);
	zassert_equal(uc_value_from_double(OBJECT_ANALOG_INPUT, PROP_STATUS_FLAGS, 1.0, &v),
		      -EBADMSG);
	zassert_equal(uc_value_from_double(OBJECT_ANALOG_INPUT, PROP_OBJECT_NAME, NAN, &v),
		      -EBADMSG);
	zassert_equal(uc_value_from_double(OBJECT_ANALOG_INPUT, PROP_PRESENT_VALUE, 1.0, NULL),
		      -EINVAL);
}

/* Values the datatype of the property cannot represent: -EINVAL. */
ZTEST(uc_common, test_value_from_double_rejects)
{
	static const struct {
		uint16_t type;
		uint32_t prop;
		double value;
	} bad[] = {
		/* NaN / Inf for every numeric datatype */
		{OBJECT_ANALOG_VALUE, PROP_PRESENT_VALUE, NAN},
		{OBJECT_ANALOG_VALUE, PROP_PRESENT_VALUE, INFINITY},
		{OBJECT_ANALOG_VALUE, PROP_COV_INCREMENT, -INFINITY},
		{OBJECT_BINARY_VALUE, PROP_PRESENT_VALUE, NAN},
		{OBJECT_MULTI_STATE_VALUE, PROP_PRESENT_VALUE, NAN},
		{OBJECT_MULTI_STATE_VALUE, PROP_PRESENT_VALUE, INFINITY},
		{OBJECT_ANALOG_INPUT, PROP_OUT_OF_SERVICE, NAN},
		{OBJECT_ANALOG_INPUT, PROP_UNITS, INFINITY},
		{OBJECT_DEVICE, PROP_UTC_OFFSET, -INFINITY},
		/* REAL overflow */
		{OBJECT_ANALOG_VALUE, PROP_PRESENT_VALUE, 1e39},
		{OBJECT_ANALOG_VALUE, PROP_PRESENT_VALUE, -1e39},
		/* binary PV: 0 or 1 only */
		{OBJECT_BINARY_OUTPUT, PROP_PRESENT_VALUE, 2.0},
		{OBJECT_BINARY_OUTPUT, PROP_PRESENT_VALUE, 0.5},
		{OBJECT_BINARY_OUTPUT, PROP_PRESENT_VALUE, -1.0},
		{OBJECT_BINARY_VALUE, PROP_RELINQUISH_DEFAULT, 5.0},
		{OBJECT_BINARY_INPUT, PROP_PRIORITY_ARRAY, 0.1},
		/* BOOLEAN: 0 or 1 only */
		{OBJECT_ANALOG_INPUT, PROP_OUT_OF_SERVICE, 2.0},
		{OBJECT_ANALOG_INPUT, PROP_OUT_OF_SERVICE, -1.0},
		/* UNSIGNED: integral, 0..UINT32_MAX */
		{OBJECT_MULTI_STATE_VALUE, PROP_PRESENT_VALUE, 3.7},
		{OBJECT_MULTI_STATE_VALUE, PROP_PRESENT_VALUE, -1.0},
		{OBJECT_MULTI_STATE_VALUE, PROP_PRESENT_VALUE, -0.5},
		{OBJECT_MULTI_STATE_INPUT, PROP_NUMBER_OF_STATES, 4294967296.0},
		/* ENUMERATED: integral, 0..UINT32_MAX */
		{OBJECT_ANALOG_INPUT, PROP_UNITS, 62.5},
		{OBJECT_ANALOG_INPUT, PROP_UNITS, -1.0},
		{OBJECT_ANALOG_INPUT, PROP_UNITS, 4294967296.0},
		/* SIGNED: integral, INT32 range */
		{OBJECT_DEVICE, PROP_UTC_OFFSET, -60.9},
		{OBJECT_DEVICE, PROP_UTC_OFFSET, 2147483648.0},
		{OBJECT_DEVICE, PROP_UTC_OFFSET, -2147483649.0},
	};
	BACNET_APPLICATION_DATA_VALUE v;

	for (size_t i = 0; i < ARRAY_SIZE(bad); i++) {
		zassert_equal(uc_value_from_double(bad[i].type, bad[i].prop, bad[i].value, &v),
			      -EINVAL, "entry %u accepted", (unsigned int)i);
	}

	/* limits that are still valid */
	zassert_ok(uc_value_from_double(OBJECT_DEVICE, PROP_UTC_OFFSET, -2147483648.0, &v));
	zassert_equal(v.type.Signed_Int, INT32_MIN);
	zassert_ok(uc_value_from_double(OBJECT_MULTI_STATE_VALUE, PROP_PRESENT_VALUE, -0.0, &v));
	zassert_equal(v.type.Unsigned_Int, 0);
	zassert_ok(uc_value_from_double(OBJECT_BINARY_VALUE, PROP_PRESENT_VALUE, -0.0, &v));
	zassert_equal(v.type.Enumerated, BINARY_INACTIVE);
}

ZTEST(uc_common, test_strlcpy)
{
	char buf[5];

	zassert_ok(uc_strlcpy(buf, "abcd", sizeof(buf)));
	zassert_str_equal(buf, "abcd");
	zassert_equal(uc_strlcpy(buf, "abcde", sizeof(buf)), -ENOSPC);
	zassert_str_equal(buf, "abcd");
	zassert_ok(uc_strlcpy(buf, NULL, sizeof(buf)));
	zassert_str_equal(buf, "");
	zassert_equal(uc_strlcpy(buf, "x", 0), -ENOSPC);
}

ZTEST(uc_common, test_name_validators)
{
	zassert_true(uc_app_name_valid("thermostat"));
	zassert_true(uc_app_name_valid("a_1-b"));
	zassert_true(uc_app_name_valid("abcdefghijklmnopqrstuvw"));   /* 23 */
	zassert_false(uc_app_name_valid("abcdefghijklmnopqrstuvwx")); /* 24 */
	zassert_false(uc_app_name_valid(""));
	zassert_false(uc_app_name_valid("Thermo"));
	zassert_false(uc_app_name_valid("a.b"));
	zassert_false(uc_app_name_valid(NULL));

	zassert_true(uc_key_valid("setpoint", 23));
	zassert_true(uc_key_valid("A.b-c_9", 23));
	zassert_true(uc_key_valid("...", 23));
	zassert_false(uc_key_valid(".", 23));
	zassert_false(uc_key_valid("..", 23));
	zassert_false(uc_key_valid("", 23));
	zassert_false(uc_key_valid("a b", 23));
	zassert_false(uc_key_valid("a/b", 31));
	zassert_true(uc_key_valid("abcdefghijklmnopqrstuvwxyz01234", 31));   /* 31 */
	zassert_false(uc_key_valid("abcdefghijklmnopqrstuvwxyz012345", 31)); /* 32 */
	zassert_false(uc_key_valid("abcdefghijklmnopqrstuvwx", 23));         /* 24 */
}

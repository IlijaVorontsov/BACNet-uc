/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host tests of the header-only SDK helpers (uc_util.h, uc_point.h,
 * uc_libc.h math helpers) against the host stub.
 */

#include <stdlib.h>

#include "uc_point.h"
#include "uc_test.h"
#include "uc_util.h"

UC_APP_DECLARE()

static void test_parse_u32(void)
{
	const char *end;
	uint32_t v;

	UC_CHECK(uc_parse_u32("0", &end, &v) && v == 0 && *end == '\0');
	UC_CHECK(uc_parse_u32("4294967295x", &end, &v) && v == 4294967295u && *end == 'x');
	UC_CHECK(!uc_parse_u32("4294967296", &end, &v));
	UC_CHECK(!uc_parse_u32("99999999999999999999", &end, &v));
	UC_CHECK(!uc_parse_u32("-1", &end, &v));
	UC_CHECK(!uc_parse_u32("+1", &end, &v));
	UC_CHECK(!uc_parse_u32("", &end, &v));
	UC_CHECK(uc_str_to_u32("  42 ", &v) && v == 42);
	UC_CHECK(!uc_str_to_u32("42 43", &v));
	UC_CHECK(!uc_str_to_u32("0x10", &v));
}

static void check_parse(const char *s, double rel_tol)
{
	double ref = strtod(s, NULL);
	double v;

	if (!uc_str_to_double(s, &v)) {
		uc_test_fail(__FILE__, __LINE__, s);
		return;
	}
	if (fabs(v - ref) > fabs(ref) * rel_tol) {
		fprintf(stderr, "    %s: %.17g vs strtod %.17g\n", s, v, ref);
		uc_test_fail(__FILE__, __LINE__, "uc_parse_double accuracy");
	}
}

static void test_parse_double_matches_strtod(void)
{
	/* up to 15 significant digits and |exponent| <= 22: correctly rounded */
	static const char *const exact[] = {
		"0",
		"-0",
		"1",
		"+1",
		"-2.5",
		".5",
		"5.",
		"1e3",
		"1E-3",
		"-1.25e+2",
		"3.14159265358979",
		"0.1",
		"0.3",
		"123456.789",
		"99.99",
		"21.5",
		"1e22",
		"4194302",
		"-273.15",
		"1e-22",
		"0.000001",
		NULL,
	};
	/* long mantissas and large exponents: a few ulp */
	static const char *const approx[] = {
		"1e-300",
		"2.2250738585072014e-308",
		"1.7976931348623e308",
		"12345678901234567890123",
		"0.000000000000000000000000001",
		"3.141592653589793238",
		NULL,
	};
	static const char *const bad[] = {
		"",    "-",    "+",    ".",   "e3",    "1e",     "1e+", "inf",
		"nan", "0x10", "1..2", "--1", "1e999", "-1e999", NULL,
	};
	double v;

	for (int i = 0; exact[i] != NULL; i++) {
		check_parse(exact[i], 0.0);
	}
	for (int i = 0; approx[i] != NULL; i++) {
		check_parse(approx[i], 1e-14);
	}
	for (int i = 0; bad[i] != NULL; i++) {
		if (uc_str_to_double(bad[i], &v)) {
			uc_test_fail(__FILE__, __LINE__, bad[i]);
		}
	}
	UC_CHECK(!uc_str_to_double("1.5x", &v));
	UC_CHECK(uc_str_to_double(" \t-7.25\t", &v) && v == -7.25);
}

static void test_next_token(void)
{
	const char *p = "  ab\tcd  efghijklmnop ";
	char tok[8];

	UC_CHECK(uc_next_token(&p, tok, sizeof(tok)) && strcmp(tok, "ab") == 0);
	UC_CHECK(uc_next_token(&p, tok, sizeof(tok)) && strcmp(tok, "cd") == 0);
	UC_CHECK(!uc_next_token(&p, tok, sizeof(tok))); /* too long */
	p = "   ";
	UC_CHECK(!uc_next_token(&p, tok, sizeof(tok)));
}

static void test_param_getters(void)
{
	static const char *const words[] = {"a", "b", NULL};
	uint32_t u;
	double d;

	uc_stub_param_set("n", "17");
	uc_stub_param_set("big", "70000");
	uc_stub_param_set("x", "2.5");
	uc_stub_param_set("dev", "local");
	uc_stub_param_set("dev2", "4194303");
	uc_stub_param_set("w", "b");
	UC_CHECK_INT(uc_param_u32("n", 0, 100, 5, &u), 1);
	UC_CHECK_INT(u, 17);
	UC_CHECK_INT(uc_param_u32("missing", 0, 100, 5, &u), 0);
	UC_CHECK_INT(u, 5);
	UC_CHECK_INT(uc_param_u32("big", 0, 65535, 5, &u), UC_ERR_INVALID);
	UC_CHECK_INT(u, 5);
	UC_CHECK_LOG(UC_LOG_WRN, "param big=\"70000\": expected integer 0..65535, using 5", 1);
	UC_CHECK_INT(uc_param_double("x", 0, 10, 1, &d), 1);
	UC_CHECK_NEAR(d, 2.5, 0);
	UC_CHECK_INT(uc_param_double("x", 3, 10, 1, &d), UC_ERR_INVALID);
	UC_CHECK_INT(uc_param_device("dev", 7, &u), 1);
	UC_CHECK(u == UC_DEVICE_LOCAL);
	UC_CHECK_INT(uc_param_device("dev2", 7, &u), UC_ERR_INVALID);
	UC_CHECK_INT(u, 7);
	UC_CHECK_INT(uc_param_choice("w", words, 0, &u), 1);
	UC_CHECK_INT(u, 1);
}

static void test_math_helpers(void)
{
	UC_CHECK_NEAR(uc_clamp(5, 0, 1), 1, 0);
	UC_CHECK_NEAR(uc_clamp(-5, 0, 1), 0, 0);
	UC_CHECK_NEAR(uc_clamp(NAN, 0, 1), 0, 0);
	UC_CHECK_NEAR(uc_fmin(NAN, 2), 2, 0);
	UC_CHECK_NEAR(uc_fmax(1, NAN), 1, 0);
	UC_CHECK_NEAR(uc_rint(2.5), 2, 0);
	UC_CHECK_NEAR(uc_floor(-1.5), -2, 0);
}

static void test_point_cov_then_poll(void)
{
	struct uc_point p;

	uc_stub_remote_add(1001, 0, 1, 20.0);
	UC_CHECK_INT(uc_stub_start(), 0); /* accept events */
	uc_point_setup(&p, 1001, 0, 1, 5000);
	UC_CHECK(uc_point_start(&p, 0) >= 0);
	UC_CHECK(!uc_point_fresh(&p, 0, 0));
	/* the initial notification (delivered by hand: no on_cov here) */
	UC_CHECK(uc_point_on_cov(&p, p.sub_id, 0, 1, 20.0, 10));
	UC_CHECK(!uc_point_on_cov(&p, p.sub_id + 1, 0, 1, 20.0, 10));
	UC_CHECK(!uc_point_on_cov(&p, p.sub_id, 0, 2, 20.0, 10));
	UC_CHECK(uc_point_fresh(&p, 10, 1000));
	UC_CHECK_INT(uc_point_tick(&p, 4000), 0);
	uc_stub_remote_set(1001, 0, 1, 21.0, false);
	UC_CHECK_INT(uc_point_tick(&p, 5010), 1);
	UC_CHECK_NEAR(p.value, 21.0, 0);
	UC_CHECK_INT(p.polls, 1);
	UC_CHECK(!uc_point_fresh(&p, 20000, 10000));
	uc_point_stop(&p);
	UC_CHECK_INT(uc_stub_subs_active(), 0);
}

static void test_point_subscribe_retry(void)
{
	struct uc_point p;

	uc_stub_obj_add(0, 5, "x", 3.0);
	UC_CHECK_INT(uc_stub_start(), 0);
	uc_stub_fail(UC_STUB_FN_COV_SUBSCRIBE, UC_ERR_BUSY, 1);
	uc_point_setup(&p, UC_DEVICE_LOCAL, 0, 5, 1000);
	UC_CHECK_INT(uc_point_start(&p, 0), UC_ERR_BUSY);
	UC_CHECK_INT(uc_point_tick(&p, 0), 1); /* polled at once */
	UC_CHECK_NEAR(p.value, 3.0, 0);
	UC_CHECK_INT(uc_point_tick(&p, 29999), 1);
	UC_CHECK(p.sub_id < 0);
	(void)uc_point_tick(&p, 30000);
	UC_CHECK(p.sub_id >= 0);
}

int main(void)
{
	UC_RUN(test_parse_u32);
	UC_RUN(test_parse_double_matches_strtod);
	UC_RUN(test_next_token);
	UC_RUN(test_param_getters);
	UC_RUN(test_math_helpers);
	UC_RUN(test_point_cov_then_poll);
	UC_RUN(test_point_subscribe_retry);
	return uc_test_summary();
}

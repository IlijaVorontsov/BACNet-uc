/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * uc_test.h - minimal unit test harness for host-stub tests. Include it in
 * exactly one file of a test executable.
 *
 *   static void test_something(void) { UC_CHECK(x == 1); }
 *   int main(void) { UC_RUN(test_something); return uc_test_summary(); }
 *
 * Every test starts from uc_stub_reset() with the natively linked
 * application; a running application is stopped after the test. On a
 * failed check the captured application log is printed.
 */
#ifndef UC_TEST_H
#define UC_TEST_H

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "uc_stub.h"

static int uc_test_failed_checks;
static int uc_test_current_failed;
static int uc_test_count;
static int uc_test_failed_tests;

static inline void uc_test_fail(const char *file, int line, const char *msg)
{
	fprintf(stderr, "    %s:%d: check failed: %s\n", file, line, msg);
	uc_test_failed_checks++;
	uc_test_current_failed = 1;
}

#define UC_CHECK(cond)                                                                             \
	do {                                                                                       \
		if (!(cond)) {                                                                     \
			uc_test_fail(__FILE__, __LINE__, #cond);                                   \
		}                                                                                  \
	} while (0)

#define UC_CHECK_INT(a, b)                                                                         \
	do {                                                                                       \
		long long a_ = (long long)(a);                                                     \
		long long b_ = (long long)(b);                                                     \
		if (a_ != b_) {                                                                    \
			char m_[256];                                                              \
			snprintf(m_, sizeof(m_), "%s == %s (%lld != %lld)", #a, #b, a_, b_);       \
			uc_test_fail(__FILE__, __LINE__, m_);                                      \
		}                                                                                  \
	} while (0)

#define UC_CHECK_NEAR(a, b, tol)                                                                   \
	do {                                                                                       \
		double a_ = (double)(a);                                                           \
		double b_ = (double)(b);                                                           \
		if (!(fabs(a_ - b_) <= (tol))) {                                                   \
			char m_[256];                                                              \
			snprintf(m_, sizeof(m_), "%s ~= %s (%.6g vs %.6g, tol %g)", #a, #b, a_,    \
				 b_, (double)(tol));                                               \
			uc_test_fail(__FILE__, __LINE__, m_);                                      \
		}                                                                                  \
	} while (0)

/** Log lines at level (0: any) containing substr. */
#define UC_CHECK_LOG(level, substr, expected)                                                      \
	UC_CHECK_INT(uc_stub_log_matches((level), (substr)), (expected))

static inline void uc_test_run(const char *name, void (*fn)(void))
{
	uc_stub_set_app(uc_stub_native_app());
	uc_stub_reset();
	uc_test_current_failed = 0;
	uc_test_count++;
	fn();
	if (uc_test_current_failed) {
		uc_test_failed_tests++;
		fprintf(stderr, "FAIL %s (t=%llu ms), application log:\n", name,
			(unsigned long long)uc_stub_now());
		uc_stub_log_dump();
	} else {
		printf("ok   %s\n", name);
	}
	uc_stub_stop();
}

#define UC_RUN(fn) uc_test_run(#fn, fn)

static inline int uc_test_summary(void)
{
	printf("%d tests, %d failed (%d failed checks)\n", uc_test_count, uc_test_failed_tests,
	       uc_test_failed_checks);
	return (uc_test_failed_tests == 0) ? 0 : 1;
}

#endif /* UC_TEST_H */

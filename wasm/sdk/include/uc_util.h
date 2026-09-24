/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * uc_util.h - header-only helpers for BACnet-uc WebAssembly applications:
 * formatted logging, strict number parsing, typed parameter access and
 * object type names. Everything is static inline; only what a module uses
 * ends up in it. Works unchanged in native host-stub builds.
 */
#ifndef UC_UTIL_H
#define UC_UTIL_H

#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "bacnet_uc.h"
#include "uc_libc.h"

#ifdef __cplusplus
extern "C" {
#endif

#define UC_ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

/** Longest log line the host keeps; longer lines are truncated. */
#define UC_LOG_LINE_MAX 120

/** Largest valid BACnet object/device instance (4194303 is reserved). */
#define UC_INSTANCE_MAX 4194302u

/** Largest BACnet object type number. */
#define UC_OBJ_TYPE_MAX 1023u

/** Longest parameter value (apps.schema.json). */
#define UC_PARAM_VALUE_MAX 95

/* ---------------------------------------------------------------------- */
/* Logging                                                                 */
/* ---------------------------------------------------------------------- */

static inline void uc_vlogf(int32_t level, const char *fmt, va_list ap)
{
	char line[UC_LOG_LINE_MAX + 1];
	int n = vsnprintf(line, sizeof(line), fmt, ap);

	if (n <= 0) {
		return;
	}
	if ((size_t)n >= sizeof(line)) {
		n = (int)sizeof(line) - 1;
	}
	uc_log(level, line, (uint32_t)n);
}

/** printf-style log line (formatted with libc-builtin vsnprintf). */
static inline void uc_logf(int32_t level, const char *fmt, ...)
	__attribute__((format(printf, 2, 3)));

static inline void uc_logf(int32_t level, const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	uc_vlogf(level, fmt, ap);
	va_end(ap);
}

/* ---------------------------------------------------------------------- */
/* Strings and numbers (no locale, no libc import)                         */
/* ---------------------------------------------------------------------- */

static inline bool uc_is_blank(char c)
{
	return (c == ' ') || (c == '\t');
}

static inline bool uc_is_digit(char c)
{
	return (c >= '0') && (c <= '9');
}

static inline bool uc_streq(const char *a, const char *b)
{
	while ((*a != '\0') && (*a == *b)) {
		a++;
		b++;
	}
	return *a == *b;
}

static inline const char *uc_skip_blanks(const char *s)
{
	while (uc_is_blank(*s)) {
		s++;
	}
	return s;
}

/**
 * Parse a decimal unsigned integer (digits only, no sign) that fits in 32
 * bits. On success *end points behind the last digit.
 */
static inline bool uc_parse_u32(const char *s, const char **end, uint32_t *out)
{
	uint64_t v = 0;
	const char *p = s;

	if (!uc_is_digit(*p)) {
		return false;
	}
	while (uc_is_digit(*p)) {
		v = v * 10u + (uint64_t)(*p - '0');
		if (v > UINT32_MAX) {
			return false;
		}
		p++;
	}
	*out = (uint32_t)v;
	if (end != NULL) {
		*end = p;
	}
	return true;
}

/* 10^n for 0 <= n, by binary powering (exact up to 10^22). */
static inline double uc_pow10(uint32_t n)
{
	double result = 1.0;
	double base = 10.0;

	while (n != 0u) {
		if ((n & 1u) != 0u) {
			result *= base;
		}
		base *= base;
		n >>= 1;
	}
	return result;
}

/**
 * Parse a finite decimal floating point number:
 *   [+-] digits [ . [digits] ] [ (e|E) [+-] digits ]   or   [+-] . digits ...
 * No hex floats, no inf/nan. Up to 19 significant digits are used exactly;
 * the result is within a few ulp of the correctly rounded value. On
 * success *end points behind the number.
 */
static inline bool uc_parse_double(const char *s, const char **end, double *out)
{
	const char *p = s;
	bool neg = false;
	uint64_t mant = 0;
	int32_t exp10 = 0;
	int digits = 0;
	int sig = 0;
	double v;

	if ((*p == '+') || (*p == '-')) {
		neg = (*p == '-');
		p++;
	}
	while (uc_is_digit(*p)) {
		if (sig < 19) {
			if ((mant != 0u) || (*p != '0')) {
				sig++;
			}
			mant = mant * 10u + (uint64_t)(*p - '0');
		} else {
			exp10++;
		}
		digits++;
		p++;
	}
	if (*p == '.') {
		p++;
		while (uc_is_digit(*p)) {
			if (sig < 19) {
				if ((mant != 0u) || (*p != '0')) {
					sig++;
				}
				mant = mant * 10u + (uint64_t)(*p - '0');
				exp10--;
			}
			digits++;
			p++;
		}
	}
	if (digits == 0) {
		return false;
	}
	if ((*p == 'e') || (*p == 'E')) {
		const char *q = p + 1;
		bool eneg = false;
		uint32_t e = 0;

		if ((*q == '+') || (*q == '-')) {
			eneg = (*q == '-');
			q++;
		}
		if (!uc_is_digit(*q)) {
			return false;
		}
		while (uc_is_digit(*q)) {
			if (e < 100000u) {
				e = e * 10u + (uint32_t)(*q - '0');
			}
			q++;
		}
		exp10 += eneg ? -(int32_t)e : (int32_t)e;
		p = q;
	}

	v = (double)mant;
	if ((mant != 0u) && (exp10 != 0)) {
		if (exp10 < -400) {
			v = 0.0;
		} else if (exp10 > 400) {
			v = __builtin_inf();
		} else if (exp10 < 0) {
			/* two steps keep 10^n finite for tiny values */
			if (exp10 < -300) {
				v /= uc_pow10(300u);
				exp10 += 300;
			}
			v /= uc_pow10((uint32_t)-exp10);
		} else {
			v *= uc_pow10((uint32_t)exp10);
		}
	}
	if (!__builtin_isfinite(v)) {
		return false;
	}
	*out = neg ? -v : v;
	if (end != NULL) {
		*end = p;
	}
	return true;
}

/** Whole-string u32: optional surrounding blanks, nothing else. */
static inline bool uc_str_to_u32(const char *s, uint32_t *out)
{
	const char *end;
	uint32_t v;

	s = uc_skip_blanks(s);
	if (!uc_parse_u32(s, &end, &v) || (*uc_skip_blanks(end) != '\0')) {
		return false;
	}
	*out = v;
	return true;
}

/** Whole-string double: optional surrounding blanks, nothing else. */
static inline bool uc_str_to_double(const char *s, double *out)
{
	const char *end;
	double v;

	s = uc_skip_blanks(s);
	if (!uc_parse_double(s, &end, &v) || (*uc_skip_blanks(end) != '\0')) {
		return false;
	}
	*out = v;
	return true;
}

/**
 * Copy the next blank-separated token of *p into tok (NUL-terminated) and
 * advance *p behind it. Returns false at the end of the string or when the
 * token does not fit into size bytes.
 */
static inline bool uc_next_token(const char **p, char *tok, uint32_t size)
{
	const char *s = uc_skip_blanks(*p);
	uint32_t n = 0;

	if (*s == '\0') {
		*p = s;
		return false;
	}
	while ((*s != '\0') && !uc_is_blank(*s)) {
		if (n + 1u >= size) {
			return false;
		}
		tok[n++] = *s++;
	}
	tok[n] = '\0';
	*p = s;
	return true;
}

/* ---------------------------------------------------------------------- */
/* Parameters                                                              */
/* ---------------------------------------------------------------------- */

/*
 * Typed parameter getters. Return 1 when the parameter is present and
 * valid (*out set to it), 0 when it is missing (*out = fallback) and a
 * negative UC_ERR_* when it is present but invalid (*out = fallback, a
 * warning naming the key is logged).
 */

static inline int32_t uc_param_text(const char *key, char *buf, uint32_t size)
{
	int32_t rc = uc_param_str(key, buf, size);

	if (rc == UC_ERR_NOT_FOUND) {
		return 0;
	}
	if (rc < 0) {
		uc_logf(UC_LOG_WRN, "param %s: too long or unreadable (%d)", key, (int)rc);
		return rc;
	}
	return 1;
}

static inline int32_t uc_param_u32(const char *key, uint32_t lo, uint32_t hi, uint32_t fallback,
				   uint32_t *out)
{
	char buf[UC_PARAM_VALUE_MAX + 1];
	int32_t rc = uc_param_text(key, buf, sizeof(buf));
	uint32_t v;

	*out = fallback;
	if (rc <= 0) {
		return rc;
	}
	if (!uc_str_to_u32(buf, &v) || (v < lo) || (v > hi)) {
		uc_logf(UC_LOG_WRN, "param %s=\"%s\": expected integer %u..%u, using %u", key, buf,
			(unsigned int)lo, (unsigned int)hi, (unsigned int)fallback);
		return UC_ERR_INVALID;
	}
	*out = v;
	return 1;
}

static inline int32_t uc_param_double(const char *key, double lo, double hi, double fallback,
				      double *out)
{
	char buf[UC_PARAM_VALUE_MAX + 1];
	int32_t rc = uc_param_text(key, buf, sizeof(buf));
	double v;

	*out = fallback;
	if (rc <= 0) {
		return rc;
	}
	if (!uc_str_to_double(buf, &v) || (v < lo) || (v > hi)) {
		uc_logf(UC_LOG_WRN, "param %s=\"%s\": expected number %g..%g, using %g", key, buf,
			lo, hi, fallback);
		return UC_ERR_INVALID;
	}
	*out = v;
	return 1;
}

/** Device parameter: "local" (UC_DEVICE_LOCAL) or an instance 0..4194302. */
static inline int32_t uc_param_device(const char *key, uint32_t fallback, uint32_t *out)
{
	char buf[UC_PARAM_VALUE_MAX + 1];
	int32_t rc = uc_param_text(key, buf, sizeof(buf));
	uint32_t v;

	*out = fallback;
	if (rc <= 0) {
		return rc;
	}
	if (uc_streq(uc_skip_blanks(buf), "local")) {
		*out = UC_DEVICE_LOCAL;
		return 1;
	}
	if (!uc_str_to_u32(buf, &v) || (v > UC_INSTANCE_MAX)) {
		uc_logf(UC_LOG_WRN, "param %s=\"%s\": expected \"local\" or 0..%u", key, buf,
			(unsigned int)UC_INSTANCE_MAX);
		return UC_ERR_INVALID;
	}
	*out = v;
	return 1;
}

/**
 * Keyword parameter: *out is the index of the value in words (NULL
 * terminated list), fallback when missing or not in the list.
 */
static inline int32_t uc_param_choice(const char *key, const char *const *words, uint32_t fallback,
				      uint32_t *out)
{
	char buf[UC_PARAM_VALUE_MAX + 1];
	int32_t rc = uc_param_text(key, buf, sizeof(buf));

	*out = fallback;
	if (rc <= 0) {
		return rc;
	}
	for (uint32_t i = 0; words[i] != NULL; i++) {
		if (uc_streq(buf, words[i])) {
			*out = i;
			return 1;
		}
	}
	uc_logf(UC_LOG_WRN, "param %s=\"%s\": unknown value, using \"%s\"", key, buf,
		words[fallback]);
	return UC_ERR_INVALID;
}

/* ---------------------------------------------------------------------- */
/* BACnet helpers                                                          */
/* ---------------------------------------------------------------------- */

/** Short type name for log lines ("AI", "BV", ...; "OBJ" otherwise). */
static inline const char *uc_obj_type_abbr(uint32_t type)
{
	switch (type) {
	case UC_OBJ_ANALOG_INPUT:
		return "AI";
	case UC_OBJ_ANALOG_OUTPUT:
		return "AO";
	case UC_OBJ_ANALOG_VALUE:
		return "AV";
	case UC_OBJ_BINARY_INPUT:
		return "BI";
	case UC_OBJ_BINARY_OUTPUT:
		return "BO";
	case UC_OBJ_BINARY_VALUE:
		return "BV";
	case UC_OBJ_DEVICE:
		return "DEV";
	case UC_OBJ_MULTI_STATE_INPUT:
		return "MSI";
	case UC_OBJ_MULTI_STATE_OUTPUT:
		return "MSO";
	case UC_OBJ_MULTI_STATE_VALUE:
		return "MSV";
	default:
		return "OBJ";
	}
}

static inline bool uc_obj_is_binary(uint32_t type)
{
	return (type == UC_OBJ_BINARY_INPUT) || (type == UC_OBJ_BINARY_OUTPUT) ||
	       (type == UC_OBJ_BINARY_VALUE);
}

/** Types uc_obj_create() accepts: AI AO AV BI BO BV MSI MSO MSV. */
static inline bool uc_obj_is_creatable(uint32_t type)
{
	return (type <= UC_OBJ_BINARY_VALUE) || (type == UC_OBJ_MULTI_STATE_INPUT) ||
	       (type == UC_OBJ_MULTI_STATE_OUTPUT) || (type == UC_OBJ_MULTI_STATE_VALUE);
}

/** Write a Present_Value locally (device == UC_DEVICE_LOCAL) or remotely. */
static inline int32_t uc_pv_write_dev(uint32_t device, uint32_t type, uint32_t instance,
					double value, uint32_t priority, uint32_t timeout_ms)
{
	if (device == UC_DEVICE_LOCAL) {
		return uc_pv_write(type, instance, value, priority);
	}
	return uc_remote_write(device, type, instance, UC_PROP_PRESENT_VALUE, UC_ARRAY_ALL, value,
			       priority, timeout_ms);
}

/** Relinquish a Present_Value priority locally or remotely. */
static inline int32_t uc_pv_relinquish_dev(uint32_t device, uint32_t type, uint32_t instance,
					     uint32_t priority, uint32_t timeout_ms)
{
	if (device == UC_DEVICE_LOCAL) {
		return uc_prop_write_null(type, instance, UC_PROP_PRESENT_VALUE, priority);
	}
	return uc_remote_write_null(device, type, instance, UC_PROP_PRESENT_VALUE, priority,
				    timeout_ms);
}

/** Read a Present_Value locally or remotely. */
static inline int32_t uc_pv_read_dev(uint32_t device, uint32_t type, uint32_t instance,
				       double *out, uint32_t timeout_ms)
{
	if (device == UC_DEVICE_LOCAL) {
		return uc_pv_read(type, instance, out);
	}
	return uc_remote_read(device, type, instance, UC_PROP_PRESENT_VALUE, UC_ARRAY_ALL, out,
			      timeout_ms);
}

#ifdef __cplusplus
}
#endif

#endif /* UC_UTIL_H */

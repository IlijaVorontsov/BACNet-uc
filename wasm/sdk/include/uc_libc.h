/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * uc_libc.h - C library subset for BACnet-uc WebAssembly applications.
 *
 * Applications are linked with -nostdlib: there is no libc in the module.
 * The functions declared here are provided at run time by WAMR's
 * libc-builtin library (import module "env"; the firmware builds WAMR with
 * WAMR_BUILD_LIBC_BUILTIN=1). uc-cc allows exactly these names to stay
 * undefined at link time (sdk/tools/uc_abi.py, LIBC_BUILTIN); any other
 * undefined function is a link error, and uc-cc also checks that every
 * "env" import has the signature WAMR registers for it.
 *
 * Differences from ISO C (libc_builtin_wrapper.c, WAMR 2.4.5):
 *   - printf/vprintf/puts/putchar output is collected per line and logged
 *     like uc_log() at info level (tagged "uc_app: <name>:", sanitised,
 *     cut to 120 characters, rate limited; an unfinished line is flushed
 *     when the callback returns). Prefer uc_log()/uc_logf() (uc_util.h),
 *     which also select the level.
 *   - Formatting: %d %i %u %x %X %o %c %s %p %e %f %g and flags/width/
 *     precision are supported. %ld/%lu/%zu are 32 bit (wasm32 long);
 *     %lld/%llu/%jd are 64 bit. The number is formatted by the firmware's
 *     snprintf (floating point needs CONFIG_CBPRINTF_FP_SUPPORT, which the
 *     BACnet-uc prj.conf enables).
 *   - A bad pointer or an unterminated string argument traps the
 *     application (WAMR validates every pointer argument).
 *   - malloc/calloc/realloc/free/strdup allocate from the app heap
 *     (apps.json "heap_kb", default 8 KiB, 0 disables it: then they
 *     return NULL).
 *   - strtol/strtoul: endptr must not be NULL. There is no strtod; use
 *     uc_parse_double() from uc_util.h.
 *   - abort/exit are not available (their WAMR signatures differ from
 *     ISO C). Use uc_trap().
 *
 * Math: the uc_* helpers at the end compile to single WebAssembly
 * instructions. Other <math.h> functions (sin, exp, pow, fmod, round, ...)
 * are not provided and fail to link.
 *
 * Native builds (host-stub unit tests, !__wasm__) include the host C
 * library headers instead.
 */
#ifndef UC_LIBC_H
#define UC_LIBC_H

#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#if !defined(__wasm__)

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#else /* __wasm__ */

#ifdef __cplusplus
extern "C" {
#endif

/* stdio: console output and formatting */
int printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
int vprintf(const char *fmt, va_list ap);
int snprintf(char *buf, size_t size, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
int vsnprintf(char *buf, size_t size, const char *fmt, va_list ap);
int puts(const char *s);
int putchar(int c);

/* string.h */
void *memcpy(void *dst, const void *src, size_t n);
void *memmove(void *dst, const void *src, size_t n);
void *memset(void *s, int c, size_t n);
int memcmp(const void *a, const void *b, size_t n);
void *memchr(const void *s, int c, size_t n);
size_t strlen(const char *s);
int strcmp(const char *a, const char *b);
int strncmp(const char *a, const char *b, size_t n);
int strncasecmp(const char *a, const char *b, size_t n);
char *strcpy(char *dst, const char *src);
char *strncpy(char *dst, const char *src, size_t n);
char *strchr(const char *s, int c);
char *strstr(const char *haystack, const char *needle);
size_t strspn(const char *s, const char *accept);
size_t strcspn(const char *s, const char *reject);
char *strdup(const char *s);

/* stdlib.h */
void *malloc(size_t size);
void *calloc(size_t n, size_t size);
void *realloc(void *p, size_t size);
void free(void *p);
int atoi(const char *s);
long strtol(const char *s, char **endptr, int base);
unsigned long strtoul(const char *s, char **endptr, int base);

/* ctype.h */
int isalnum(int c);
int isalpha(int c);
int isdigit(int c);
int isgraph(int c);
int isprint(int c);
int isspace(int c);
int isupper(int c);
int isxdigit(int c);
int tolower(int c);
int toupper(int c);

#ifdef __cplusplus
}
#endif

#endif /* __wasm__ */

/* ---------------------------------------------------------------------- */
/* Helpers that need no import                                             */
/* ---------------------------------------------------------------------- */

/** Stop the application with a trap (WebAssembly "unreachable"). The host
 *  records the trap and puts the app into the "failed" state. */
static inline void uc_trap(void)
{
	__builtin_trap();
}

/* f64.abs, f64.sqrt, f64.floor, f64.ceil, f64.trunc, f64.nearest */
static inline double uc_fabs(double x)
{
	return __builtin_fabs(x);
}

static inline double uc_sqrt(double x)
{
	return __builtin_sqrt(x);
}

static inline double uc_floor(double x)
{
	return __builtin_floor(x);
}

static inline double uc_ceil(double x)
{
	return __builtin_ceil(x);
}

static inline double uc_trunc(double x)
{
	return __builtin_trunc(x);
}

/** Round half to even (f64.nearest). */
static inline double uc_rint(double x)
{
	return __builtin_rint(x);
}

static inline bool uc_isnan(double x)
{
	return __builtin_isnan(x);
}

static inline bool uc_isfinite(double x)
{
	return __builtin_isfinite(x);
}

/** Smaller of two values; a NaN argument yields the other one (C fmin). */
static inline double uc_fmin(double a, double b)
{
	if (__builtin_isnan(a)) {
		return b;
	}
	if (__builtin_isnan(b)) {
		return a;
	}
	return (a < b) ? a : b;
}

/** Larger of two values; a NaN argument yields the other one (C fmax). */
static inline double uc_fmax(double a, double b)
{
	if (__builtin_isnan(a)) {
		return b;
	}
	if (__builtin_isnan(b)) {
		return a;
	}
	return (a > b) ? a : b;
}

/** Clamp x to [lo, hi]; NaN yields lo. */
static inline double uc_clamp(double x, double lo, double hi)
{
	if (!(x >= lo)) {
		return lo;
	}
	return (x > hi) ? hi : x;
}

#endif /* UC_LIBC_H */

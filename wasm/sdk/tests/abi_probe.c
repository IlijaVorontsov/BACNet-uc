/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * ABI probe: references every host function of bacnet_uc.h and every
 * libc-builtin function of uc_libc.h, so that the import section of the
 * compiled module shows the signatures clang derives from the headers.
 * sdk/tests/test_abi.py compares them with sdk/tools/uc_abi.py, the
 * firmware's native symbol table and WAMR's libc-builtin table.
 * Never executed.
 */

#include <bacnet_uc.h>
#include <uc_libc.h>

UC_APP_DECLARE()

static char buf[64];
static double d;
volatile int32_t sink;

UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
	char *end;
	void *p;

	uc_log(UC_LOG_INF, buf, 1);
	sink += (int32_t)uc_uptime_ms();
	sink += uc_set_tick_period(100);
	sink += uc_param_get(buf, 1, buf, sizeof(buf));
	sink += uc_param_get_number(buf, 1, &d);
	sink += uc_obj_create(2, 1, buf, 1);
	sink += uc_obj_delete(2, 1);
	sink += uc_prop_read(2, 1, 85, -1, &d);
	sink += uc_prop_write(2, 1, 85, -1, d, 0);
	sink += uc_prop_write_null(2, 1, 85, 8);
	sink += uc_prop_write_string(2, 1, 77, buf, 1);
	sink += uc_remote_read(1, 2, 1, 85, -1, &d, 100);
	sink += uc_remote_write(1, 2, 1, 85, -1, d, 8, 100);
	sink += uc_remote_write_null(1, 2, 1, 85, 8, 100);
	sink += uc_cov_subscribe(1, 2, 1, 300);
	sink += uc_cov_unsubscribe(0);
	sink += uc_io_find(buf, 1);
	sink += uc_io_read(0, &d);
	sink += uc_io_write(0, d);
	sink += uc_kv_get(buf, 1, buf, sizeof(buf));
	sink += uc_kv_set(buf, 1, buf, 1);

	/* libc-builtin: volatile function pointers keep clang from folding */
	sink += printf(buf, 1);
	sink += snprintf(buf, sizeof(buf), buf, d);
	sink += puts(buf);
	sink += putchar(sink);
	{
		int (*volatile f_vprintf)(const char *, va_list) = vprintf;
		int (*volatile f_vsnprintf)(char *, size_t, const char *, va_list) = vsnprintf;
		void *(*volatile f_memcpy)(void *, const void *, size_t) = memcpy;
		void *(*volatile f_memmove)(void *, const void *, size_t) = memmove;
		void *(*volatile f_memset)(void *, int, size_t) = memset;
		int (*volatile f_memcmp)(const void *, const void *, size_t) = memcmp;
		void *(*volatile f_memchr)(const void *, int, size_t) = memchr;
		size_t (*volatile f_strlen)(const char *) = strlen;
		int (*volatile f_strcmp)(const char *, const char *) = strcmp;
		int (*volatile f_strncmp)(const char *, const char *, size_t) = strncmp;
		int (*volatile f_strncasecmp)(const char *, const char *, size_t) = strncasecmp;
		char *(*volatile f_strcpy)(char *, const char *) = strcpy;
		char *(*volatile f_strncpy)(char *, const char *, size_t) = strncpy;
		char *(*volatile f_strchr)(const char *, int) = strchr;
		char *(*volatile f_strstr)(const char *, const char *) = strstr;
		size_t (*volatile f_strspn)(const char *, const char *) = strspn;
		size_t (*volatile f_strcspn)(const char *, const char *) = strcspn;
		char *(*volatile f_strdup)(const char *) = strdup;
		void *(*volatile f_malloc)(size_t) = malloc;
		void *(*volatile f_calloc)(size_t, size_t) = calloc;
		void *(*volatile f_realloc)(void *, size_t) = realloc;
		void (*volatile f_free)(void *) = free;
		int (*volatile f_atoi)(const char *) = atoi;
		long (*volatile f_strtol)(const char *, char **, int) = strtol;
		unsigned long (*volatile f_strtoul)(const char *, char **, int) = strtoul;
		int (*volatile f_ctype[])(int) = {isalnum, isalpha, isdigit,  isgraph, isprint,
						  isspace, isupper, isxdigit, tolower, toupper};

		sink += (int32_t)(uintptr_t)f_vprintf + (int32_t)(uintptr_t)f_vsnprintf;
		p = f_memcpy(buf, buf + 1, 2);
		p = f_memmove(p, buf, 2);
		p = f_memset(p, 0, 2);
		sink += f_memcmp(buf, p, 2);
		p = f_memchr(buf, 1, 2);
		sink += (int32_t)f_strlen(buf) + f_strcmp(buf, p) + f_strncmp(buf, p, 1);
		sink += f_strncasecmp(buf, p, 1);
		p = f_strcpy(buf, p);
		p = f_strncpy(buf, p, 2);
		p = f_strchr(buf, 'a');
		p = f_strstr(buf, p);
		sink += (int32_t)(f_strspn(buf, p) + f_strcspn(buf, p));
		p = f_strdup(buf);
		p = f_realloc(p, 10);
		f_free(p);
		p = f_calloc(2, 3);
		f_free(f_malloc(4));
		sink += f_atoi(buf) + (int32_t)f_strtol(buf, &end, 10) +
			(int32_t)f_strtoul(end, &end, 16);
		for (unsigned int i = 0; i < sizeof(f_ctype) / sizeof(f_ctype[0]); i++) {
			sink += f_ctype[i](sink);
		}
	}
	return (int32_t)(uintptr_t)p;
}

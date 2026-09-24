/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Zephyr glue for WAMR's "zephyr" platform layer.
 *
 * wamr_zephyr_vprintf(): WAMR's os_printf()/os_vprintf() (runtime
 * diagnostics, BH_VPRINTF) as one printk() per call. The platform's
 * default is vprintf() through the stdout hook that bh_platform_init()
 * installs, which emits one printk("%c") per character; with
 * CONFIG_LOG_PRINTK every character would become a log message.
 *
 * __stdout_hook_install(): bh_platform_init() calls it unconditionally.
 * Zephyr's minimal libc and picolibc provide it; an external C library
 * (native_sim on the host libc) does not, and its printf() already writes
 * to the host's stdout. Built only with CONFIG_EXTERNAL_LIBC.
 */

#include <stdarg.h>

#include <zephyr/sys/printk.h>
#include <zephyr/toolchain.h>

#include "wamr_zephyr_glue.h"

int wamr_zephyr_vprintf(const char *format, va_list ap)
{
	vprintk(format, ap);
	return 0;
}

#if defined(CONFIG_EXTERNAL_LIBC)
void __stdout_hook_install(int (*hook)(int c));

void __stdout_hook_install(int (*hook)(int c))
{
	ARG_UNUSED(hook);
}
#endif

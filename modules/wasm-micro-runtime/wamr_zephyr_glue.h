/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Force-included into WAMR's core/shared/platform/zephyr/zephyr_platform.c,
 * which calls BH_VPRINTF (= wamr_zephyr_vprintf) without a prototype.
 */
#ifndef WAMR_ZEPHYR_GLUE_H_
#define WAMR_ZEPHYR_GLUE_H_

#include <stdarg.h>

int wamr_zephyr_vprintf(const char *format, va_list ap);

#endif /* WAMR_ZEPHYR_GLUE_H_ */

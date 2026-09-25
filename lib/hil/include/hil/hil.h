/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * lib/hil: HIL rig instrumentation of instrumented DUT images (docs/HIL.md 8.3).
 */
#ifndef HIL_HIL_H_
#define HIL_HIL_H_

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** hil_mark() level argument: invert the marker. */
#define HIL_MARK_TOGGLE (-1)

/** Number of markers (hil-marker-gpios of /zephyr,user; 0 without the hil board overlay). */
size_t hil_marker_count(void);

/**
 * Set marker @p n to 0 or 1, or invert it with HIL_MARK_TOGGLE.
 *
 * @return the new level (0 or 1), -ENOENT for an unknown marker, or a negative GPIO error.
 */
int hil_mark(size_t n, int level);

/** Reset cause (hwinfo RESET_* bits) read at boot, before lib/hil cleared it. */
uint32_t hil_boot_reset_cause(void);

/** UID as lower-case hex (hwinfo device id), "-" if the board has none. */
const char *hil_uid_hex(void);

/**
 * First IPv4 address of the default interface as text, or "-" without one.
 *
 * @return @p buf
 */
char *hil_ipv4_str(char *buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* HIL_HIL_H_ */

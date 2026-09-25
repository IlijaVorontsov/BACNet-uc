/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Runner side of zephyr_memmap.c (no WAMR platform headers needed).
 */
#ifndef UC_RUNNER_ZEPHYR_MEMMAP_API_H_
#define UC_RUNNER_ZEPHYR_MEMMAP_API_H_

#include <stdint.h>

/** Size of the last linear memory block allocated from the WAMR pool. */
uint64_t uc_runner_linear_block_bytes(void);

#endif /* UC_RUNNER_ZEPHYR_MEMMAP_API_H_ */

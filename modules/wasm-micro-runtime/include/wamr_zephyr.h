/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Zephyr-specific services of the WAMR glue for the embedder.
 */
#ifndef WAMR_ZEPHYR_H_
#define WAMR_ZEPHYR_H_

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Prepare the WAMR pool [start, start + size) for AOT code, which WAMR
 * loads into memory from the pool and executes there.
 *
 * - ARM MPU (PMSAv7 and PMSAv8): checks, from the MPU registers, that
 *   privileged code may execute every 32-byte granule of the range (the
 *   enabled region that decides the access has XN clear, or no region
 *   covers it and the default memory map allows execution). With
 *   clear_xn, the XN attribute of the enabled regions that decide the
 *   access to the range is cleared first (these regions become executable
 *   as a whole). Dynamic regions (stack guards, memory domains) are
 *   reprogrammed by the kernel on a context switch; only the static
 *   regions matter for a pool, and they are not reprogrammed after boot.
 * - native_sim (host libc): mprotect(PROT_READ | PROT_WRITE | PROT_EXEC) of
 *   the pages of the range; clear_xn is not needed.
 * - No memory protection: nothing to do.
 *
 * Call it from a thread before the first AOT module is loaded.
 *
 * @retval 0 the range is executable
 * @retval -EACCES the range is not executable (and clear_xn was false or
 *         did not help, e.g. overlapping regions)
 * @retval -ENOTSUP memory protection this function does not know
 * @retval <0 other negative errno (mprotect())
 */
int wamr_zephyr_exec_enable(void *start, size_t size, bool clear_xn);

/**
 * Make WAMR's os_mmap() (AOT sections, linear memories) return 16-byte
 * aligned blocks from the WAMR pool instead of the pool allocator's 8-byte
 * alignment; AOT code may depend on its section alignment (x86-64 movaps
 * on 16-byte constants). Call after wasm_runtime_full_init() and before the
 * first module is loaded.
 */
void wamr_zephyr_mmap_align_init(void);

#ifdef __cplusplus
}
#endif

#endif /* WAMR_ZEPHYR_H_ */

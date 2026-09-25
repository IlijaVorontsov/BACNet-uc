/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Force-included into WAMR's core/iwasm/common/wasm_memory.c of the runner
 * (after the firmware's modules/wasm-micro-runtime/wamr_linear_memory.h,
 * which maps os_mmap to the firmware's rounding allocator). The other
 * memory map functions of that file go to the Zephyr platform emulation in
 * zephyr_memmap.c, so that linear memories come from the WAMR pool as on
 * the target instead of from Linux mmap(), which maps whole pages.
 */
#ifndef UC_RUNNER_ZEPHYR_MEMMAP_H_
#define UC_RUNNER_ZEPHYR_MEMMAP_H_

#define os_munmap   uc_runner_zephyr_os_munmap
#define os_mremap   uc_runner_zephyr_os_mremap
#define os_mprotect uc_runner_zephyr_os_mprotect

#endif /* UC_RUNNER_ZEPHYR_MEMMAP_H_ */

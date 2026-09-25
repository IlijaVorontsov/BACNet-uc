/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Zephyr-specific services of the WAMR glue for the embedder.
 */
#ifndef WAMR_ZEPHYR_H_
#define WAMR_ZEPHYR_H_

#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>

#include <wasm_export.h>

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

/**
 * Module code that wasm_runtime_instantiate() would run itself, outside
 * any call the embedder makes (and so outside its watchdog, instruction
 * budget and exec env user data): a start function, or an exported
 * "__wasm_call_ctors", "__post_instantiate" or "_initialize" function.
 *
 * @return "start function" or the export's name, NULL when instantiating
 *         the module runs no module code
 */
const char *wamr_zephyr_instantiate_code(wasm_module_t module);

/**
 * Output of WAMR's os_printf()/os_vprintf(): the libc-builtin printf,
 * vprintf, puts and putchar of the modules and the runtime's diagnostics.
 * The hook gets every call first (in the calling thread) and returns true
 * when it consumed the output; otherwise the output goes to printk() as
 * without a hook. Set it before the first module runs; NULL removes it.
 */
typedef bool (*wamr_zephyr_print_hook_t)(const char *format, va_list ap);
void wamr_zephyr_set_print_hook(wamr_zephyr_print_hook_t hook);

#ifdef __cplusplus
}
#endif

#endif /* WAMR_ZEPHYR_H_ */

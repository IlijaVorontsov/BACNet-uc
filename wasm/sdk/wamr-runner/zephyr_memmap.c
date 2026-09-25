/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Linear memory allocation of the Zephyr platform (WAMR 2.4.5,
 * core/shared/platform/zephyr/zephyr_platform.c) for the runner:
 * os_mmap() = BH_MALLOC (the WAMR pool) + memset, os_munmap() = BH_FREE,
 * os_mremap() = os_mremap_slow(), os_mprotect() does nothing.
 *
 * Only wasm_memory.c uses these (zephyr_memmap.h). Its os_mmap() calls go
 * to the firmware's wamr_uc_linear_memory_mmap()
 * (modules/wasm-micro-runtime/wamr_linear_memory.c), which the runner
 * compiles with os_mmap -> uc_runner_zephyr_os_mmap, so the runner runs
 * the firmware's allocation path, including its page rounding.
 *
 * uc_runner_linear_block_bytes() reports the size of the last linear
 * memory block, for the check "allocated >= bounds-checked".
 */

#include <stdint.h>
#include <string.h>

#include "bh_platform.h"

#include "zephyr_memmap_api.h"

void *uc_runner_zephyr_os_mmap(void *hint, size_t size, int prot, int flags, os_file_handle file);
void uc_runner_zephyr_os_munmap(void *addr, size_t size);
void *uc_runner_zephyr_os_mremap(void *old_addr, size_t old_size, size_t new_size);
int uc_runner_zephyr_os_mprotect(void *addr, size_t size, int prot);

static uint64_t last_block;

void *uc_runner_zephyr_os_mmap(void *hint, size_t size, int prot, int flags, os_file_handle file)
{
	void *addr;

	(void)hint;
	(void)prot;
	(void)flags;
	(void)file;
	if ((uint64_t)size >= UINT32_MAX) {
		return NULL;
	}
	addr = BH_MALLOC((uint32_t)size);
	if (addr != NULL) {
		memset(addr, 0, size);
		last_block = size;
	}
	return addr;
}

void uc_runner_zephyr_os_munmap(void *addr, size_t size)
{
	(void)size;
	BH_FREE(addr);
}

void *uc_runner_zephyr_os_mremap(void *old_addr, size_t old_size, size_t new_size)
{
	void *new_mem = uc_runner_zephyr_os_mmap(NULL, new_size, 0, 0, 0);

	if (new_mem == NULL) {
		return NULL;
	}
	memcpy(new_mem, old_addr, (new_size < old_size) ? new_size : old_size);
	uc_runner_zephyr_os_munmap(old_addr, old_size);
	return new_mem;
}

int uc_runner_zephyr_os_mprotect(void *addr, size_t size, int prot)
{
	(void)addr;
	(void)size;
	(void)prot;
	return 0;
}

uint64_t uc_runner_linear_block_bytes(void)
{
	return last_block;
}

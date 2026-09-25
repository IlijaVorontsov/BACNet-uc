/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Linear memory allocation that covers the range WAMR bounds-checks.
 *
 * WAMR 2.4.5, wasm_allocate_linear_memory() (core/iwasm/common/
 * wasm_memory.c), without OS_ENABLE_HW_BOUND_CHECK:
 *
 *     map_size = init_page_count * num_bytes_per_page;
 *     *memory_data_size = align_as_and_cast(init_page_count *
 *                                           num_bytes_per_page,
 *                                           os_getpagesize());
 *     *data = wasm_mmap_linear_memory(map_size, *memory_data_size);
 *
 * memory_data_size becomes the instance's bounds (memory_data_end,
 * mem_bound_check_*, app address validation), map_size is what os_mmap()
 * allocates. On POSIX, mmap() maps whole pages, so the two agree. The
 * Zephyr platform's os_mmap() is BH_MALLOC(size) from the runtime pool:
 * a linear memory whose size is not a multiple of os_getpagesize() (4096
 * without an MMU) is bounds-checked up to 4095 bytes beyond its pool
 * block. Sizes that are not page multiples are common: a memory shrunk to
 * __heap_base (WASM_ENABLE_SHRUNK_MEMORY) plus the app heap, a one-page
 * memory with the app heap appended (num_bytes_per_page += heap_size), or
 * a memory of zero pages that gets a heap-sized page.
 *
 * The fix rounds every linear memory allocation up to the page size, so
 * the block always covers memory_data_size, whatever the module and the
 * heap size. The extra bytes are zeroed like the rest (os_mmap() clears
 * the whole block).
 *
 * memory.grow (wasm_enlarge_memory_internal()) is consistent with it: it
 * copies the old memory_data_size bytes (covered by the rounded block) into
 * a block of exactly the new size from the platform's os_mremap(), and sets
 * memory_data_size to that unrounded new size. (A memory whose page size
 * is not a multiple of 4096 cannot grow: WAMR only enlarges
 * num_bytes_per_page for memories with init == max pages.)
 */

#include <stddef.h>
#include <stdint.h>

#include "bh_platform.h"

void *wamr_uc_linear_memory_mmap(void *hint, size_t size, int prot, int flags,
				 os_file_handle file);

void *wamr_uc_linear_memory_mmap(void *hint, size_t size, int prot, int flags,
				 os_file_handle file)
{
	size_t page = (size_t)os_getpagesize();

	if ((page == 0U) || ((page & (page - 1U)) != 0U) || (size > SIZE_MAX - (page - 1U))) {
		return NULL;
	}

	return os_mmap(hint, (size + page - 1U) & ~(page - 1U), prot, flags, file);
}

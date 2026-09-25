/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Aligned os_mmap() blocks for AOT (wamr_zephyr.h).
 *
 * The Zephyr platform's os_mmap() takes its blocks from the WAMR pool
 * (BH_MALLOC), which aligns to 8 bytes. On a POSIX platform mmap() returns
 * page-aligned memory, and AOT code relies on the alignment of the
 * sections it is loaded into: x86-64 code (native_sim) reads 16-byte
 * constants of its text section with movaps, which faults on an address
 * that is only 8-byte aligned. The platform's set_exec_mem_alloc_func()
 * hook routes every os_mmap()/os_munmap() (AOT sections and linear
 * memories) through the pair below, which returns MMAP_ALIGN-aligned blocks
 * from the same pool (at most MMAP_ALIGN + sizeof(void *) bytes more per
 * block).
 */

#include <stdint.h>

#include "bh_platform.h"
#include "wasm_export.h"

#include "wamr_zephyr.h"

#define MMAP_ALIGN 16U

static void *aligned_alloc_fn(unsigned int size)
{
	const unsigned int extra = MMAP_ALIGN + (unsigned int)sizeof(void *);
	uint8_t *raw;
	uintptr_t p;

	if (size > UINT32_MAX - extra) {
		return NULL;
	}
	raw = wasm_runtime_malloc(size + extra);
	if (raw == NULL) {
		return NULL;
	}
	p = ((uintptr_t)raw + sizeof(void *) + MMAP_ALIGN - 1U) & ~(uintptr_t)(MMAP_ALIGN - 1U);
	((void **)p)[-1] = raw;

	return (void *)p;
}

static void aligned_free_fn(void *p)
{
	if (p != NULL) {
		wasm_runtime_free(((void **)p)[-1]);
	}
}

void wamr_zephyr_mmap_align_init(void)
{
	set_exec_mem_alloc_func(aligned_alloc_fn, aligned_free_fn);
}

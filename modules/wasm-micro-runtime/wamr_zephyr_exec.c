/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Executable WAMR pool for AOT code (wamr_zephyr.h).
 *
 * WAMR 2.4.5's own attempt, disable_mpu_rasr_xn() in
 * core/shared/platform/zephyr/zephyr_platform.c, runs from
 * bh_platform_init() with WASM_ENABLE_AOT and CONFIG_ARM_MPU. On ARMv7-M it
 * executes "MPU->RASR |= ~MPU_RASR_XN_Msk" for every region 0..7 that has
 * XN set: that sets every other RASR bit (4 GiB size, all eight subregions
 * disabled, AP 0b111, TEX/S/C/B) and leaves XN set, i.e. it switches those
 * regions off (the background map then applies) instead of making them
 * executable, including regions unrelated to the pool. On ARMv8-M
 * MPU_RASR_XN_Msk does not exist and it does nothing, so SRAM stays
 * execute-never. The glue compiles zephyr_platform.c without it
 * (CMakeLists.txt) and the embedder calls wamr_zephyr_exec_enable()
 * instead: it only clears XN, only on the regions that decide the access
 * to the pool, and only when asked to.
 */

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/util.h>

#include "wamr_zephyr.h"

#if defined(CONFIG_ARCH_POSIX)

#if defined(CONFIG_EXTERNAL_LIBC)
#include <sys/mman.h>
#include <unistd.h>

int wamr_zephyr_exec_enable(void *start, size_t size, bool clear_xn)
{
	uintptr_t page = (uintptr_t)sysconf(_SC_PAGESIZE);
	uintptr_t begin = ROUND_DOWN((uintptr_t)start, page);
	uintptr_t end = ROUND_UP((uintptr_t)start + size, page);

	ARG_UNUSED(clear_xn);

	if ((start == NULL) || (size == 0U)) {
		return -EINVAL;
	}
	if (mprotect((void *)begin, end - begin, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
		return -errno;
	}

	return 0;
}
#else
int wamr_zephyr_exec_enable(void *start, size_t size, bool clear_xn)
{
	ARG_UNUSED(start);
	ARG_UNUSED(size);
	ARG_UNUSED(clear_xn);

	/* no host mprotect() without the host C library */
	return -ENOTSUP;
}
#endif /* CONFIG_EXTERNAL_LIBC */

#elif defined(CONFIG_ARM_MPU) && defined(CONFIG_CPU_CORTEX_M)

#include <cmsis_core.h>

/* Region and subregion boundaries are multiples of 32 bytes on both MPU
 * architectures. */
#define GRANULE 32U
#define REGIONS_MAX 32U

#if defined(CONFIG_ARMV8_M_BASELINE) || defined(CONFIG_ARMV8_M_MAINLINE)
#define MPU_V8 1
#else
#define MPU_V8 0
#endif

struct mpu_snapshot {
	uint32_t ctrl;
	uint32_t n;
	uint32_t rbar[REGIONS_MAX];
	uint32_t attr[REGIONS_MAX]; /* v7: RASR, v8: RLAR */
};

static void mpu_read(struct mpu_snapshot *m)
{
	unsigned int key = irq_lock();

	m->ctrl = MPU->CTRL;
	m->n = MIN((MPU->TYPE & MPU_TYPE_DREGION_Msk) >> MPU_TYPE_DREGION_Pos, REGIONS_MAX);
	for (uint32_t i = 0; i < m->n; i++) {
		MPU->RNR = i;
		m->rbar[i] = MPU->RBAR;
#if MPU_V8
		m->attr[i] = MPU->RLAR;
#else
		m->attr[i] = MPU->RASR;
#endif
	}
	irq_unlock(key);
}

/* Default memory map (privileged, PRIVDEFENA or MPU off): Code, SRAM and
 * the two RAM regions are executable, peripherals and system space not. */
static bool default_map_exec(uint32_t addr)
{
	return (addr < 0x40000000U) || ((addr >= 0x60000000U) && (addr < 0xA0000000U));
}

/* Index of the region that decides the access to addr, -1 for the
 * background map, -2 for a fault (ARMv8-M: several regions match). */
static int mpu_decider(const struct mpu_snapshot *m, uint32_t addr)
{
	int hit = -1;

	for (uint32_t i = 0; i < m->n; i++) {
#if MPU_V8
		uint32_t base = m->rbar[i] & MPU_RBAR_BASE_Msk;
		uint32_t limit = (m->attr[i] & MPU_RLAR_LIMIT_Msk) | (GRANULE - 1U);

		if (((m->attr[i] & MPU_RLAR_EN_Msk) == 0U) || (addr < base) || (addr > limit)) {
			continue;
		}
		if (hit >= 0) {
			return -2;
		}
		hit = (int)i;
#else
		uint32_t rasr = m->attr[i];
		uint32_t sz = (rasr & MPU_RASR_SIZE_Msk) >> MPU_RASR_SIZE_Pos;
		uint64_t size;
		uint32_t base;

		if (((rasr & MPU_RASR_ENABLE_Msk) == 0U) || (sz < 4U)) {
			continue;
		}
		size = 1ULL << (sz + 1U);
		base = (uint32_t)(m->rbar[i] & MPU_RBAR_ADDR_Msk & ~(uint32_t)(size - 1U));
		if ((addr < base) || ((uint64_t)addr >= (uint64_t)base + size)) {
			continue;
		}
		if ((size >= 256U) &&
		    ((rasr & BIT(MPU_RASR_SRD_Pos + (uint32_t)((addr - base) / (size / 8U)))) != 0U)) {
			continue;
		}
		/* the highest-numbered matching region wins */
		hit = (int)i;
#endif
	}

	return hit;
}

static bool region_exec(const struct mpu_snapshot *m, int i)
{
#if MPU_V8
#if defined(MPU_RLAR_PXN_Msk)
	if ((m->attr[i] & MPU_RLAR_PXN_Msk) != 0U) {
		return false;
	}
#endif
	return (m->rbar[i] & MPU_RBAR_XN_Msk) == 0U;
#else
	uint32_t ap = (m->attr[i] & MPU_RASR_AP_Msk) >> MPU_RASR_AP_Pos;

	/* AP 0b000: no access, 0b100: reserved */
	return ((m->attr[i] & MPU_RASR_XN_Msk) == 0U) && (ap != 0U) && (ap != 4U);
#endif
}

static bool granule_exec(const struct mpu_snapshot *m, uint32_t addr, int *decider)
{
	int i;

	if ((m->ctrl & MPU_CTRL_ENABLE_Msk) == 0U) {
		*decider = -1;
		return default_map_exec(addr);
	}
	i = mpu_decider(m, addr);
	*decider = i;
	if (i == -1) {
		return ((m->ctrl & MPU_CTRL_PRIVDEFENA_Msk) != 0U) && default_map_exec(addr);
	}
	if (i < 0) {
		return false;
	}

	return region_exec(m, i);
}

/* Bit mask of the regions with XN that decide the access to a granule of
 * the range; *exec tells whether the whole range is executable. */
static uint32_t range_check(const struct mpu_snapshot *m, uintptr_t begin, uintptr_t end,
			    bool *exec)
{
	uint32_t xn_regions = 0;

	*exec = true;
	for (uintptr_t a = ROUND_DOWN(begin, GRANULE); a < end; a += GRANULE) {
		int i;

		if (granule_exec(m, (uint32_t)a, &i)) {
			continue;
		}
		*exec = false;
		if (i >= 0) {
			xn_regions |= BIT(i);
		}
	}

	return xn_regions;
}

static void regions_clear_xn(uint32_t regions)
{
	unsigned int key = irq_lock();

	for (uint32_t i = 0; i < REGIONS_MAX; i++) {
		if ((regions & BIT(i)) == 0U) {
			continue;
		}
		MPU->RNR = i;
#if MPU_V8
		uint32_t rlar = MPU->RLAR;
		uint32_t rbar = MPU->RBAR;

		/* disable the region while it changes */
		MPU->RLAR = rlar & ~MPU_RLAR_EN_Msk;
		__DSB();
		MPU->RBAR = rbar & ~MPU_RBAR_XN_Msk;
		MPU->RLAR = rlar;
#else
		uint32_t rasr = MPU->RASR;

		MPU->RASR = rasr & ~MPU_RASR_ENABLE_Msk;
		__DSB();
		MPU->RASR = rasr & ~MPU_RASR_XN_Msk;
#endif
	}
	__DSB();
	__ISB();
	irq_unlock(key);
}

int wamr_zephyr_exec_enable(void *start, size_t size, bool clear_xn)
{
	static struct mpu_snapshot m;
	uintptr_t begin = (uintptr_t)start;
	uintptr_t end = begin + size;
	uint32_t xn_regions;
	bool exec;

	if ((start == NULL) || (size == 0U) || (end < begin)) {
		return -EINVAL;
	}

	mpu_read(&m);
	xn_regions = range_check(&m, begin, end, &exec);
	if (exec) {
		return 0;
	}
	if (!clear_xn || (xn_regions == 0U)) {
		return -EACCES;
	}

	regions_clear_xn(xn_regions);
	mpu_read(&m);
	(void)range_check(&m, begin, end, &exec);

	return exec ? 0 : -EACCES;
}

#else

int wamr_zephyr_exec_enable(void *start, size_t size, bool clear_xn)
{
	ARG_UNUSED(start);
	ARG_UNUSED(size);
	ARG_UNUSED(clear_xn);

	/* an MPU or MMU this glue does not handle; without one, RAM executes */
	return (IS_ENABLED(CONFIG_MPU) || IS_ENABLED(CONFIG_MMU)) ? -ENOTSUP : 0;
}

#endif

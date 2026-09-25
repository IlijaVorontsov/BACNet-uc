/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Force-included into WAMR's core/iwasm/common/wasm_memory.c only (see
 * CMakeLists.txt). Every os_mmap() of that file, i.e. every allocation of
 * a linear memory, goes to wamr_uc_linear_memory_mmap()
 * (wamr_linear_memory.c), which allocates the page-rounded size that WAMR
 * bounds-checks. Other os_mmap() users (AOT code and data sections) are
 * not affected.
 */
#ifndef WAMR_LINEAR_MEMORY_H_
#define WAMR_LINEAR_MEMORY_H_

#define os_mmap wamr_uc_linear_memory_mmap

#endif /* WAMR_LINEAR_MEMORY_H_ */

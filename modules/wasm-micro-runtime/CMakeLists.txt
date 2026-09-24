# SPDX-License-Identifier: Apache-2.0
#
# CMake glue for the WebAssembly Micro Runtime (WAMR).

if(NOT CONFIG_WAMR)
  return()
endif()

set(WAMR_ROOT_DIR ${ZEPHYR_WASM_MICRO_RUNTIME_MODULE_DIR})

set(WAMR_BUILD_PLATFORM "zephyr")
set(WAMR_BUILD_TARGET ${CONFIG_WAMR_BUILD_TARGET})
set(WAMR_BUILD_INTERP 1)
set(WAMR_BUILD_LIBC_BUILTIN 1)
set(WAMR_BUILD_LIBC_WASI 0)
set(WAMR_BUILD_MULTI_MODULE 0)
set(WAMR_BUILD_LIB_PTHREAD 0)
set(WAMR_BUILD_LIB_WASI_THREADS 0)
set(WAMR_BUILD_SHARED_MEMORY 0)
set(WAMR_BUILD_JIT 0)
set(WAMR_BUILD_FAST_JIT 0)
set(WAMR_BUILD_SIMD 0)
set(WAMR_BUILD_GC 0)
set(WAMR_BUILD_EXCE_HANDLING 0)
set(WAMR_BUILD_MEMORY64 0)
set(WAMR_BUILD_TAIL_CALL 0)

foreach(opt FAST_INTERP AOT BULK_MEMORY REF_TYPES THREAD_MGR)
  if(CONFIG_WAMR_${opt})
    set(WAMR_BUILD_${opt} 1)
  else()
    set(WAMR_BUILD_${opt} 0)
  endif()
endforeach()

if(CONFIG_WAMR_THREAD_MGR)
  # Without wasm threads the cluster of an exec env never spawns threads.
  # The default aux stack scheme would still split the module's aux stack
  # into (max_thread_num + 1) segments at exec env creation and leave the
  # main instance only one of them; allocating thread aux stacks from the
  # app heap instead keeps the whole aux stack for the main instance.
  add_definitions(-DWASM_ENABLE_HEAP_AUX_STACK_ALLOCATION=1)
endif()

if(NOT CONFIG_WAMR_LOG)
  add_definitions(-DWASM_ENABLE_LOG=0)
endif()

# os_printf()/os_vprintf() as one printk() per call (wamr_zephyr_glue.c).
add_definitions(-DBH_VPRINTF=wamr_zephyr_vprintf)

# Static stacks of the platform layer for threads WAMR creates itself.
add_definitions(-DBH_ZEPHYR_MPU_STACK_COUNT=${CONFIG_WAMR_OS_THREAD_STACKS}
                -DBH_ZEPHYR_MPU_STACK_SIZE=${CONFIG_WAMR_OS_THREAD_STACK_SIZE})

include(${WAMR_ROOT_DIR}/build-scripts/runtime_lib.cmake)

zephyr_library_named(wamr)
zephyr_library_sources(${WAMR_RUNTIME_LIB_SOURCE}
                       ${CMAKE_CURRENT_LIST_DIR}/wamr_zephyr_glue.c)
# WAMR's sources are not warning-clean under Zephyr's default flags.
zephyr_library_compile_options(-Wno-unused-variable -Wno-unused-function
                               -Wno-sign-compare -Wno-unused-but-set-variable
                               -Wno-double-promotion)
# invokeNative_*.s carry no .note.GNU-stack section; on native_sim the host
# linker would otherwise mark the stack executable.
zephyr_library_compile_options($<$<COMPILE_LANGUAGE:ASM>:-Wa,--noexecstack>)
if(CONFIG_WAMR_AOT)
  # aot_reloc_thumb.c (WAMR 2.4.5) adds a void * to an int32; GCC >= 14
  # rejects the implicit pointer-to-integer conversion by default. The
  # arithmetic is the intended one on these 32-bit targets.
  zephyr_library_compile_options(-Wno-int-conversion -Wno-pointer-arith)
endif()
# zephyr_platform.c calls BH_VPRINTF without a prototype.
set_source_files_properties(
  ${WAMR_ROOT_DIR}/core/shared/platform/zephyr/zephyr_platform.c
  PROPERTIES COMPILE_OPTIONS "-include;${CMAKE_CURRENT_LIST_DIR}/wamr_zephyr_glue.h")
# ems_gc.c redefines Zephyr's GB() macro (no GCC option controls that
# warning); the file only has content with WAMR_BUILD_GC.
set_source_files_properties(${WAMR_ROOT_DIR}/core/shared/mem-alloc/ems/ems_gc.c
                            PROPERTIES COMPILE_OPTIONS "-w")
zephyr_include_directories(${WAMR_ROOT_DIR}/core/iwasm/include)

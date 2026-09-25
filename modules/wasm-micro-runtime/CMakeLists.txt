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

if(CONFIG_WAMR_INSTRUCTION_LIMIT GREATER 0)
  set(WAMR_BUILD_INSTRUCTION_METERING 1)
endif()

# os_printf()/os_vprintf() as one printk() per call (wamr_zephyr_glue.c).
add_definitions(-DBH_VPRINTF=wamr_zephyr_vprintf)

include(${WAMR_ROOT_DIR}/build-scripts/runtime_lib.cmake)

zephyr_library_named(wamr)
zephyr_library_sources(${WAMR_RUNTIME_LIB_SOURCE}
                       ${CMAKE_CURRENT_LIST_DIR}/wamr_zephyr_glue.c
                       ${CMAKE_CURRENT_LIST_DIR}/wamr_zephyr_exec.c
                       ${CMAKE_CURRENT_LIST_DIR}/wamr_zephyr_mmap.c
                       ${CMAKE_CURRENT_LIST_DIR}/wamr_zephyr_module.c
                       ${CMAKE_CURRENT_LIST_DIR}/wamr_linear_memory.c)

# wamr_zephyr_instantiate_code() (wamr_zephyr_module.c) knows the exports
# WAMR 2.4.5 calls during instantiation. Stop the build when a WAMR update
# looks up others.
foreach(uc_wamr_rt ${WAMR_ROOT_DIR}/core/iwasm/interpreter/wasm_runtime.c
                   ${WAMR_ROOT_DIR}/core/iwasm/aot/aot_runtime.c)
  file(STRINGS ${uc_wamr_rt} uc_wamr_post
       REGEX "lookup_post_instantiate_func\\(module_inst, *\"")
  string(REGEX MATCHALL "\"[A-Za-z0-9_]+\"" uc_wamr_post "${uc_wamr_post}")
  if(NOT uc_wamr_post STREQUAL "\"_initialize\";\"__post_instantiate\";\"__wasm_call_ctors\"")
    message(FATAL_ERROR "${uc_wamr_rt} runs other functions at instantiation than "
            "WAMR 2.4.5 (${uc_wamr_post}): re-check "
            "modules/wasm-micro-runtime/wamr_zephyr_module.c")
  endif()
endforeach()

# Linear memory allocation (wamr_linear_memory.c): WAMR bounds-checks the
# linear memory rounded up to os_getpagesize() but the Zephyr platform's
# os_mmap() allocates only the unrounded size from the pool. Every os_mmap()
# of wasm_memory.c is redirected to an allocator that rounds up. The check
# below stops the build when a WAMR update changes the code this relies on.
set(uc_wamr_memory_c ${WAMR_ROOT_DIR}/core/iwasm/common/wasm_memory.c)
file(STRINGS ${uc_wamr_memory_c} uc_wamr_round
     REGEX "align_as_and_cast\\(\\*memory_data_size, page_size\\)")
file(STRINGS ${uc_wamr_memory_c} uc_wamr_map
     REGEX "map_size = init_page_count \\* num_bytes_per_page;")
file(STRINGS ${uc_wamr_memory_c} uc_wamr_mmap REGEX "os_mmap\\(")
if(NOT uc_wamr_round OR NOT uc_wamr_map OR NOT uc_wamr_mmap)
  message(FATAL_ERROR "WAMR linear memory allocation changed (${uc_wamr_memory_c}): "
          "re-check modules/wasm-micro-runtime/wamr_linear_memory.c")
endif()
set_source_files_properties(${uc_wamr_memory_c} PROPERTIES COMPILE_OPTIONS
  "-include;${CMAKE_CURRENT_LIST_DIR}/wamr_linear_memory.h")
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
# zephyr_platform.c calls BH_VPRINTF without a prototype. Its only use of
# WASM_ENABLE_AOT is disable_mpu_rasr_xn(), which corrupts the MPU setup
# (see wamr_zephyr_exec.c); compiled with WASM_ENABLE_AOT=0 it is left out.
set(uc_wamr_platform_c ${WAMR_ROOT_DIR}/core/shared/platform/zephyr/zephyr_platform.c)
file(STRINGS ${uc_wamr_platform_c} uc_wamr_aot_uses REGEX "WASM_ENABLE_AOT")
list(LENGTH uc_wamr_aot_uses uc_wamr_aot_uses)
if(NOT uc_wamr_aot_uses EQUAL 2)
  message(FATAL_ERROR "${uc_wamr_platform_c} uses WASM_ENABLE_AOT differently than "
          "WAMR 2.4.5: re-check modules/wasm-micro-runtime/CMakeLists.txt")
endif()
set_source_files_properties(${uc_wamr_platform_c} PROPERTIES COMPILE_OPTIONS
  "-include;${CMAKE_CURRENT_LIST_DIR}/wamr_zephyr_glue.h;-UWASM_ENABLE_AOT;-DWASM_ENABLE_AOT=0")
# ems_gc.c redefines Zephyr's GB() macro (no GCC option controls that
# warning); the file only has content with WAMR_BUILD_GC.
set_source_files_properties(${WAMR_ROOT_DIR}/core/shared/mem-alloc/ems/ems_gc.c
                            PROPERTIES COMPILE_OPTIONS "-w")
zephyr_include_directories(${WAMR_ROOT_DIR}/core/iwasm/include
                           ${CMAKE_CURRENT_LIST_DIR}/include)

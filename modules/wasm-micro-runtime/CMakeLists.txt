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
set(WAMR_BUILD_THREAD_MGR 0)
set(WAMR_BUILD_LIB_PTHREAD 0)
set(WAMR_BUILD_SHARED_MEMORY 0)
set(WAMR_BUILD_JIT 0)
set(WAMR_BUILD_FAST_JIT 0)
set(WAMR_BUILD_SIMD 0)
set(WAMR_BUILD_GC 0)
set(WAMR_BUILD_EXCE_HANDLING 0)
set(WAMR_BUILD_MEMORY64 0)
set(WAMR_BUILD_TAIL_CALL 0)

foreach(opt FAST_INTERP AOT BULK_MEMORY REF_TYPES)
  if(CONFIG_WAMR_${opt})
    set(WAMR_BUILD_${opt} 1)
  else()
    set(WAMR_BUILD_${opt} 0)
  endif()
endforeach()

if(NOT CONFIG_WAMR_LOG)
  add_definitions(-DWASM_ENABLE_LOG=0)
endif()

include(${WAMR_ROOT_DIR}/build-scripts/runtime_lib.cmake)

zephyr_library_named(wamr)
zephyr_library_sources(${WAMR_RUNTIME_LIB_SOURCE})
# WAMR's sources are not warning-clean under Zephyr's default flags.
zephyr_library_compile_options(-Wno-unused-variable -Wno-unused-function
                               -Wno-sign-compare -Wno-unused-but-set-variable)
zephyr_include_directories(${WAMR_ROOT_DIR}/core/iwasm/include)

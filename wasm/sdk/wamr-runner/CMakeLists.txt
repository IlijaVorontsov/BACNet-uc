# SPDX-License-Identifier: Apache-2.0
#
# Host build of WAMR with the firmware's configuration plus
#   uc-wamr-runner        scenario runner for BACnet-uc modules (.wasm, x86-64 .aot)
#   libuc_bacnet_stub.so  the "bacnet_uc" host functions as an iwasm --native-lib
# See build.sh.

cmake_minimum_required(VERSION 3.14)
project(uc_wamr_runner C)

set(UC_SDK_DIR ${CMAKE_CURRENT_LIST_DIR}/..)
if(NOT DEFINED WAMR_ROOT)
  # west workspace: <top>/BACNet-uc/wasm/sdk/wamr-runner -> <top>/modules/lib/wamr
  get_filename_component(WAMR_ROOT ${CMAKE_CURRENT_LIST_DIR}/../../../../modules/lib/wamr
                         ABSOLUTE)
endif()
if(NOT EXISTS ${WAMR_ROOT}/build-scripts/runtime_lib.cmake)
  message(FATAL_ERROR "WAMR not found at ${WAMR_ROOT}; pass -DWAMR_ROOT=<wamr source>")
endif()

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# Same feature set as modules/wasm-micro-runtime/CMakeLists.txt with the
# BACnet-uc prj.conf (CONFIG_WAMR_FAST_INTERP=y, CONFIG_WAMR_THREAD_MGR=y,
# bulk memory and reference types on). The AOT loader is on here so that
# x86-64 AOT files (uc-aot --board native_sim/native/64) can be run.
set(WAMR_BUILD_PLATFORM "linux")
set(WAMR_BUILD_TARGET "X86_64")
set(WAMR_BUILD_INTERP 1)
set(WAMR_BUILD_FAST_INTERP 1)
set(WAMR_BUILD_AOT 1)
set(WAMR_BUILD_JIT 0)
set(WAMR_BUILD_FAST_JIT 0)
set(WAMR_BUILD_LIBC_BUILTIN 1)
set(WAMR_BUILD_LIBC_WASI 0)
set(WAMR_BUILD_MULTI_MODULE 0)
set(WAMR_BUILD_LIB_PTHREAD 0)
set(WAMR_BUILD_LIB_WASI_THREADS 0)
set(WAMR_BUILD_SHARED_MEMORY 0)
set(WAMR_BUILD_SIMD 0)
set(WAMR_BUILD_GC 0)
set(WAMR_BUILD_EXCE_HANDLING 0)
set(WAMR_BUILD_MEMORY64 0)
set(WAMR_BUILD_TAIL_CALL 0)
set(WAMR_BUILD_BULK_MEMORY 1)
set(WAMR_BUILD_REF_TYPES 1)
set(WAMR_BUILD_THREAD_MGR 1)
add_definitions(-DWASM_ENABLE_HEAP_AUX_STACK_ALLOCATION=1)
# Zephyr has no hardware bounds checks: software checks as on the target.
set(WAMR_DISABLE_HW_BOUND_CHECK 1)

set(WAMR_ROOT_DIR ${WAMR_ROOT})
include(${WAMR_ROOT_DIR}/build-scripts/runtime_lib.cmake)

add_library(vmlib STATIC ${WAMR_RUNTIME_LIB_SOURCE})
set_target_properties(vmlib PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_link_libraries(vmlib PUBLIC m dl pthread)
# invokeNative_em64.s has no .note.GNU-stack section
target_compile_options(vmlib PRIVATE $<$<COMPILE_LANGUAGE:ASM>:-Wa,--noexecstack>)

set(UC_STUB_SRC ${UC_SDK_DIR}/host-stub/uc_stub.c)
set(UC_INCLUDES ${UC_SDK_DIR}/include ${UC_SDK_DIR}/host-stub ${CMAKE_CURRENT_LIST_DIR}
                ${WAMR_ROOT}/core/iwasm/include)
set(UC_WARNINGS -Wall -Wextra -Wno-unused-parameter)

add_executable(uc-wamr-runner runner.c natives.c scenario.c ${UC_STUB_SRC})
target_include_directories(uc-wamr-runner PRIVATE ${UC_INCLUDES})
target_compile_options(uc-wamr-runner PRIVATE ${UC_WARNINGS})
target_link_libraries(uc-wamr-runner vmlib)

# Loaded by an iwasm that exports its symbols (build.sh builds one with
# -Wl,--export-dynamic); the WAMR API is resolved from the executable.
add_library(uc_bacnet_stub MODULE native_lib.c natives.c ${UC_STUB_SRC})
target_include_directories(uc_bacnet_stub PRIVATE ${UC_INCLUDES})
target_compile_options(uc_bacnet_stub PRIVATE ${UC_WARNINGS})
set_target_properties(uc_bacnet_stub PROPERTIES PREFIX "lib" POSITION_INDEPENDENT_CODE ON)

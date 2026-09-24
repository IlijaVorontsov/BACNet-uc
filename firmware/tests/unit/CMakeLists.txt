# SPDX-License-Identifier: Apache-2.0
#
# Unit tests (ztest, native_sim/native/64) for uc_common.c and uc_config.c.
# The firmware sources are compiled unchanged; uc_storage is replaced by an
# in-memory stub (src/storage_stub.c). The JSON examples of schemas/examples
# are embedded at build time so the tests follow the documented examples.

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(bacnet_uc_unit)

set(FW_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../..)
set(EXAMPLES_DIR ${FW_DIR}/../schemas/examples)

target_include_directories(app PRIVATE ${FW_DIR}/include ${FW_DIR}/src)

target_sources(app PRIVATE
  src/storage_stub.c
  src/test_common.c
  src/test_config.c
  ${FW_DIR}/src/common/uc_common.c
  ${FW_DIR}/src/config/uc_config.c
)

set(gen_dir ${ZEPHYR_BINARY_DIR}/include/generated)
foreach(doc device io apps)
  generate_inc_file_for_target(app ${EXAMPLES_DIR}/${doc}.json
                               ${gen_dir}/example_${doc}.json.inc)
endforeach()

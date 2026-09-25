# SPDX-License-Identifier: Apache-2.0
#
# CMake helper for BACnet-uc WebAssembly applications (uses uc-cc/uc-aot,
# no cross toolchain setup needed):
#
#   include(<path to wasm/sdk>/cmake/BacnetUcWasm.cmake)
#   bacnet_uc_wasm_app(NAME thermostat
#                      SOURCES thermostat.c pid.c
#                      [INCLUDES dir...] [DEFINES NAME=VALUE...]
#                      [UCCC_ARGS arg...]          # e.g. -O2 --stack-size 8192
#                      [AOT_BOARDS board...])      # e.g. nucleo_f767zi
#
# Produces ${CMAKE_CURRENT_BINARY_DIR}/<NAME>.wasm (target <NAME>_wasm, part
# of ALL) and <NAME>.<board with / replaced by _>.aot per AOT board.

set(BACNET_UC_SDK_DIR ${CMAKE_CURRENT_LIST_DIR}/.. CACHE INTERNAL "BACnet-uc WASM SDK")

function(bacnet_uc_wasm_app)
  cmake_parse_arguments(A "" "NAME" "SOURCES;INCLUDES;DEFINES;UCCC_ARGS;AOT_BOARDS" ${ARGN})
  if(NOT A_NAME OR NOT A_SOURCES)
    message(FATAL_ERROR "bacnet_uc_wasm_app: NAME and SOURCES are required")
  endif()
  set(sdk ${BACNET_UC_SDK_DIR})
  set(out ${CMAKE_CURRENT_BINARY_DIR}/${A_NAME}.wasm)
  set(srcs)
  foreach(s ${A_SOURCES})
    get_filename_component(abs ${s} ABSOLUTE)
    list(APPEND srcs ${abs})
  endforeach()
  set(args ${A_UCCC_ARGS})
  foreach(i ${A_INCLUDES})
    get_filename_component(abs ${i} ABSOLUTE)
    list(APPEND args -I ${abs})
  endforeach()
  foreach(d ${A_DEFINES})
    list(APPEND args -D ${d})
  endforeach()
  file(GLOB sdk_headers ${sdk}/include/*.h)
  add_custom_command(
    OUTPUT ${out}
    COMMAND ${sdk}/uc-cc -q ${args} -o ${out} ${srcs}
    DEPENDS ${srcs} ${sdk_headers} ${sdk}/uc-cc
    COMMENT "uc-cc ${A_NAME}.wasm"
    VERBATIM)
  set(outputs ${out})
  foreach(board ${A_AOT_BOARDS})
    string(REPLACE "/" "_" key ${board})
    set(aot ${CMAKE_CURRENT_BINARY_DIR}/${A_NAME}.${key}.aot)
    add_custom_command(
      OUTPUT ${aot}
      COMMAND ${sdk}/uc-aot --board ${board} -o ${aot} ${out}
      DEPENDS ${out}
      COMMENT "uc-aot ${A_NAME} ${board}"
      VERBATIM)
    list(APPEND outputs ${aot})
  endforeach()
  add_custom_target(${A_NAME}_wasm ALL DEPENDS ${outputs})
endfunction()

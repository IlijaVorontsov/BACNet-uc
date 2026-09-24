# SPDX-License-Identifier: Apache-2.0
#
# Glue for Zephyr modules whose zephyr/module.yml declares cmake-ext and
# kconfig-ext but for which the Zephyr tree carries no glue. Same scheme as
# $ZEPHYR_BASE/modules/modules.cmake: every sub-directory with a
# CMakeLists.txt / Kconfig provides the glue for the module of that name.

file(GLOB uc_cmake_modules "${CMAKE_CURRENT_LIST_DIR}/*/CMakeLists.txt")
foreach(module ${uc_cmake_modules})
  get_filename_component(module_dir  ${module} DIRECTORY)
  get_filename_component(module_name ${module_dir} NAME)
  zephyr_string(SANITIZE TOUPPER MODULE_NAME_UPPER ${module_name})
  set(ZEPHYR_${MODULE_NAME_UPPER}_CMAKE_DIR ${module_dir})
endforeach()

file(GLOB uc_kconfig_modules "${CMAKE_CURRENT_LIST_DIR}/*/Kconfig")
foreach(module ${uc_kconfig_modules})
  get_filename_component(module_dir  ${module} DIRECTORY)
  get_filename_component(module_name ${module_dir} NAME)
  zephyr_string(SANITIZE TOUPPER MODULE_NAME_UPPER ${module_name})
  set(ZEPHYR_${MODULE_NAME_UPPER}_KCONFIG ${module_dir}/Kconfig)
endforeach()

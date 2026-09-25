# SPDX-License-Identifier: Apache-2.0
#
# lib/hil: built only into instrumented images (CONFIG_HIL from snippets/hil/hil.conf).

if(CONFIG_HIL)
  zephyr_include_directories(include)
  zephyr_library_named(hil)
  zephyr_library_sources(src/hil.c)
  zephyr_library_sources_ifdef(CONFIG_HIL_SHELL src/hil_shell.c)
  # HIL-owned code builds warning-free (hil/host/build.sh); the app's own flags are untouched.
  zephyr_library_compile_options(-Werror)
endif()

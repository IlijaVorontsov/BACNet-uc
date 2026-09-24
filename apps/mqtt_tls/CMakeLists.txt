# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mqtt_tls)

target_sources(app PRIVATE
  src/main.c
  src/net_wait.c
  src/mqtt_app.c
  src/device_id.c
  src/commands.c
)
target_sources_ifdef(CONFIG_APP_WATCHDOG app PRIVATE src/watchdog.c)

if(CONFIG_APP_MQTT_TLS)
  target_sources(app PRIVATE src/tls_creds.c)
  # tls_creds.c parses the credentials with mbedTLS at boot.
  target_link_libraries(app PRIVATE mbedTLS)

  # Embed the TLS credentials named in Kconfig into the image. Each file is
  # turned into a C byte list (<name>.inc) that src/tls_creds.c includes.
  # Relative paths are resolved against the application directory.
  # PEM files get a NUL terminator appended in C, which mbedTLS requires to
  # recognise the PEM format; DER files (*.der) are embedded as-is.
  set(gen_dir ${ZEPHYR_BINARY_DIR}/include/generated/app_creds)

  function(app_embed_credential kconfig_value name required)
    if("${kconfig_value}" STREQUAL "")
      if(required)
        message(FATAL_ERROR "${name}: no credential file configured")
      endif()
      return()
    endif()

    if(IS_ABSOLUTE "${kconfig_value}")
      set(path "${kconfig_value}")
    else()
      set(path "${APPLICATION_SOURCE_DIR}/${kconfig_value}")
    endif()

    if(NOT EXISTS "${path}")
      message(FATAL_ERROR
        "${name}: credential file '${path}' does not exist.\n"
        "Set the matching CONFIG_APP_MQTT_TLS_* option, or generate "
        "development credentials with apps/mqtt_tls/scripts/gen_dev_certs.sh.")
    endif()

    string(TOLOWER "${name}" lname)
    generate_inc_file_for_target(app "${path}" "${gen_dir}/${lname}.inc")
    target_compile_definitions(app PRIVATE APP_HAVE_${name}=1)

    get_filename_component(ext "${path}" LAST_EXT)
    string(TOLOWER "${ext}" ext)
    if(NOT ext STREQUAL ".der")
      target_compile_definitions(app PRIVATE APP_${name}_IS_PEM=1)
    endif()
  endfunction()

  app_embed_credential("${CONFIG_APP_MQTT_TLS_CA_CERT_FILE}" CA_CERT TRUE)

  if(CONFIG_APP_MQTT_TLS_CLIENT_AUTH)
    app_embed_credential("${CONFIG_APP_MQTT_TLS_CLIENT_CERT_FILE}" CLIENT_CERT TRUE)
    app_embed_credential("${CONFIG_APP_MQTT_TLS_CLIENT_KEY_FILE}" CLIENT_KEY TRUE)
  endif()

  target_include_directories(app PRIVATE ${gen_dir})
endif()

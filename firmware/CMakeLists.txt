# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(bacnet_uc)

target_include_directories(app PRIVATE include src)

target_sources(app PRIVATE
  src/main.c
  src/common/uc_common.c
  src/storage/uc_storage.c
  src/config/uc_config.c
  src/net/uc_net.c
  src/bacnet/uc_bn_node.c
  src/bacnet/uc_bn_local.c
  src/bacnet/uc_bn_client.c
  src/bacnet/uc_bn_cov.c
  src/io/uc_io.c
)

target_sources_ifdef(CONFIG_UC_APPS app PRIVATE
  src/apps/uc_app_mgr.c
  src/apps/uc_app_host_api.c
)

target_sources_ifdef(CONFIG_UC_MGMT app PRIVATE
  src/mgmt/uc_mgmt_util.c
  src/mgmt/uc_mgmt_app.c
  src/mgmt/uc_mgmt_io.c
  src/mgmt/uc_mgmt_node.c
)

target_sources_ifdef(CONFIG_UC_SHELL app PRIVATE
  src/shell/uc_shell.c
)

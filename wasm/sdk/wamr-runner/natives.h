/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * "bacnet_uc" native symbol table for WAMR on the host (natives.c).
 */
#ifndef UC_RUNNER_NATIVES_H
#define UC_RUNNER_NATIVES_H

#include <stdint.h>

#include <wasm_export.h>

/** Table for wasm_runtime_register_natives("bacnet_uc", ...). */
NativeSymbol *uc_runner_natives(uint32_t *count);

#endif /* UC_RUNNER_NATIVES_H */

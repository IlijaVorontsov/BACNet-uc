/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * uc-link.c - DEMO STAND-IN for the stock uc-link app. NEVER USE IT ON A
 * REAL BOARD.
 *
 * The uc-hub simulator does not run WebAssembly: it emulates uc-link in
 * Python (uc_hub.sim.network.UcLinkApp) and picks the emulation by the
 * module's file name. The demo still needs a module file, because the hub
 * uploads the module of every link and checks its sha256 as it does for real
 * hardware; hub/examples/demo/hub.yaml points drivers.bacnet_uc.uc_link_wasm
 * at the build of this file. It does nothing but refuse to start: on a real
 * board it logs an error and uc_app_init fails, so the manifest's links are
 * visibly not running. Real boards need the uc-link app built from
 * wasm/examples/uc-link on the BACnet firmware branch.
 *
 * Build: ./build.sh
 */
#include "bacnet_uc.h"

UC_APP_DECLARE()

UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
	uc_log_str(UC_LOG_ERR, "uc-link.wasm is the uc-hub demo stand-in, not the uc-link app");
	return UC_ERR_UNSUPPORTED;
}

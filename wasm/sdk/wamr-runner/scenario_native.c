/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Native scenario runner: the application source is linked in (host-stub
 * build). Same options and output as the WAMR runner, so that a scenario
 * can be run both ways and the dumps compared (validate.py).
 */

#include <stdio.h>

#include "scenario.h"
#include "uc_stub.h"

int main(int argc, char **argv)
{
	struct uc_scenario sc;
	int32_t start_rc = 0;
	int res;

	uc_stub_set_app(uc_stub_native_app());
	uc_stub_reset();
	if (uc_scenario_parse(argc, argv, &sc) < 0) {
		uc_scenario_usage(argv[0], false);
		return 2;
	}
	res = uc_scenario_run(&sc, &start_rc);
	if (sc.dump) {
		printf("start %d\n", (int)start_rc);
		uc_stub_dump(stdout);
		uc_stub_stop();
		printf("--- after stop\n");
		uc_stub_dump(stdout);
	} else {
		uc_stub_stop();
	}
	return res;
}

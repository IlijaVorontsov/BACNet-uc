/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Scenario options for the host stub (see scenario.h).
 */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "scenario.h"
#include "uc_stub.h"

void uc_scenario_usage(const char *prog, bool wasm)
{
	fprintf(stderr,
		"usage: %s [options]%s\n"
		"setup:\n"
		"  -p KEY=VALUE                  app parameter (apps.json params)\n"
		"  --perms LIST                  comma separated: bacnet.local,bacnet.remote,io,kv"
		" (default: all)\n"
		"  --period MS                   apps.json period_ms (default 1000)\n"
		"  --local-device N              local device instance (default 1000)\n"
		"  --obj TYPE:INST=VALUE[:NAME]  object configured by io.json\n"
		"  --remote DEV:TYPE:INST=VALUE  remote point\n"
		"  --io NAME=VALUE[:out]         raw IO channel\n"
		"timeline:\n"
		"  --at MS:ACTION                at MS after the start:\n"
		"      remote:DEV:TYPE:INST=VALUE  remote value change, COV notification\n"
		"      silent:DEV:TYPE:INST=VALUE  remote value change, no notification\n"
		"      fail:DEV:TYPE:INST=ERR      remote point fails with UC_ERR_* (0: ok)\n"
		"      local:TYPE:INST=VALUE       local input changes\n"
		"      write:TYPE:INST=VALUE[@PRIO] BACnet client WriteProperty\n"
		"      relinquish:TYPE:INST@PRIO   BACnet client relinquish\n"
		"      restart                     stop and start the app (kv survives)\n"
		"  --run MS                      simulated run time (default 10000)\n"
		"output:\n"
		"  --dump                        print the final state\n"
		"  -v                            print application log lines\n",
		prog, wasm ? " MODULE.wasm|MODULE.aot" : "");
	if (wasm) {
		fprintf(stderr,
			"runtime:\n"
			"  --heap BYTES                  app heap (default 8192)\n"
			"  --stack BYTES                 WAMR stack (default 4096)\n"
			"  --pool BYTES                  WAMR memory pool (default 131072)\n"
			"  --json                        print the result as JSON\n");
	}
}

static bool parse_u32(const char *s, char **end, uint32_t *out)
{
	unsigned long long v;

	errno = 0;
	v = strtoull(s, end, 10);
	if ((*end == s) || (errno != 0) || (v > UINT32_MAX)) {
		return false;
	}
	*out = (uint32_t)v;
	return true;
}

static bool parse_double(const char *s, char **end, double *out)
{
	*out = strtod(s, end);
	return *end != s;
}

/* "[DEV:]TYPE:INST" followed by sep; with_dev selects the DEV field. */
static bool parse_ref(const char **s, bool with_dev, struct uc_step *st)
{
	char *end;

	if (with_dev) {
		if (!parse_u32(*s, &end, &st->device) || (*end != ':')) {
			return false;
		}
		*s = end + 1;
	}
	if (!parse_u32(*s, &end, &st->type) || (*end != ':')) {
		return false;
	}
	*s = end + 1;
	if (!parse_u32(*s, &end, &st->instance)) {
		return false;
	}
	*s = end;
	return true;
}

/* "[DEV:]TYPE:INST=VALUE" */
static bool parse_ref_value(const char *s, bool with_dev, struct uc_step *st)
{
	char *end;

	if (!parse_ref(&s, with_dev, st) || (*s != '=')) {
		return false;
	}
	if (!parse_double(s + 1, &end, &st->value)) {
		return false;
	}
	s = end;
	st->priority = 0;
	if (*s == '@') {
		if (!parse_u32(s + 1, &end, &st->priority)) {
			return false;
		}
		s = end;
	}
	return *s == '\0' || *s == ':';
}

static int parse_step(const char *arg, struct uc_step *st)
{
	char *end;
	const char *a;
	static const struct {
		const char *prefix;
		enum uc_step_kind kind;
		bool dev;
	} kinds[] = {
		{"remote:", STEP_REMOTE, true}, {"silent:", STEP_SILENT, true},
		{"fail:", STEP_FAIL, true},     {"local:", STEP_LOCAL, false},
		{"write:", STEP_WRITE, false},  {"relinquish:", STEP_RELINQUISH, false},
	};

	memset(st, 0, sizeof(*st));
	st->at_ms = strtoull(arg, &end, 10);
	if ((end == arg) || (*end != ':')) {
		return -1;
	}
	a = end + 1;
	if (strcmp(a, "restart") == 0) {
		st->kind = STEP_RESTART;
		return 0;
	}
	for (size_t i = 0; i < sizeof(kinds) / sizeof(kinds[0]); i++) {
		size_t n = strlen(kinds[i].prefix);

		if (strncmp(a, kinds[i].prefix, n) != 0) {
			continue;
		}
		st->kind = kinds[i].kind;
		a += n;
		if (st->kind == STEP_RELINQUISH) {
			if (!parse_ref(&a, false, st) || (*a != '@') ||
			    !parse_u32(a + 1, &end, &st->priority) || (*end != '\0')) {
				return -1;
			}
			return 0;
		}
		return parse_ref_value(a, kinds[i].dev, st) ? 0 : -1;
	}
	return -1;
}

static int parse_perms(const char *s, uint32_t *perms)
{
	char buf[128];
	char *tok;
	char *save = NULL;

	snprintf(buf, sizeof(buf), "%s", s);
	*perms = 0;
	for (tok = strtok_r(buf, ",", &save); tok != NULL; tok = strtok_r(NULL, ",", &save)) {
		if (strcmp(tok, "bacnet.local") == 0) {
			*perms |= UC_STUB_PERM_LOCAL;
		} else if (strcmp(tok, "bacnet.remote") == 0) {
			*perms |= UC_STUB_PERM_REMOTE;
		} else if (strcmp(tok, "io") == 0) {
			*perms |= UC_STUB_PERM_IO;
		} else if (strcmp(tok, "kv") == 0) {
			*perms |= UC_STUB_PERM_KV;
		} else if (tok[0] != '\0') {
			return -1;
		}
	}
	return 0;
}

static int bad(const char *opt, const char *val)
{
	fprintf(stderr, "invalid %s \"%s\"\n", opt, val);
	return -1;
}

int uc_scenario_parse(int argc, char **argv, struct uc_scenario *sc)
{
	memset(sc, 0, sizeof(*sc));
	sc->run_ms = 10000;
	sc->heap = 8192;
	sc->stack = 4096;
	sc->pool = 131072;

	for (int i = 1; i < argc; i++) {
		const char *opt = argv[i];
		const char *val = (i + 1 < argc) ? argv[i + 1] : NULL;
		struct uc_step st;
		char *end;
		uint32_t u;

		if (strcmp(opt, "--dump") == 0) {
			sc->dump = true;
			continue;
		}
		if (strcmp(opt, "--json") == 0) {
			sc->json = true;
			continue;
		}
		if (strcmp(opt, "-v") == 0) {
			sc->verbose = true;
			uc_stub_set_verbose(true);
			continue;
		}
		if ((opt[0] != '-') || (opt[1] == '\0')) {
			if (sc->module != NULL) {
				return bad("argument", opt);
			}
			sc->module = opt;
			continue;
		}
		if (val == NULL) {
			return bad("option (missing value)", opt);
		}
		i++;
		if (strcmp(opt, "-p") == 0) {
			char key[32];
			const char *eq = strchr(val, '=');

			if ((eq == NULL) || ((size_t)(eq - val) >= sizeof(key))) {
				return bad(opt, val);
			}
			memcpy(key, val, (size_t)(eq - val));
			key[eq - val] = '\0';
			if (uc_stub_param_set(key, eq + 1) < 0) {
				return bad(opt, val);
			}
		} else if (strcmp(opt, "--perms") == 0) {
			if (parse_perms(val, &u) < 0) {
				return bad(opt, val);
			}
			uc_stub_set_perms(u);
		} else if (strcmp(opt, "--period") == 0) {
			if (!parse_u32(val, &end, &u) || (*end != '\0')) {
				return bad(opt, val);
			}
			uc_stub_set_period(u);
		} else if (strcmp(opt, "--local-device") == 0) {
			if (!parse_u32(val, &end, &u) || (*end != '\0')) {
				return bad(opt, val);
			}
			uc_stub_set_local_device(u);
		} else if (strcmp(opt, "--obj") == 0) {
			const char *name;

			if (!parse_ref_value(val, false, &st)) {
				return bad(opt, val);
			}
			name = strchr(strchr(val, '='), ':');
			if (uc_stub_obj_add(st.type, st.instance, (name != NULL) ? name + 1 : NULL,
					    st.value) < 0) {
				return bad(opt, val);
			}
		} else if (strcmp(opt, "--remote") == 0) {
			if (!parse_ref_value(val, true, &st) ||
			    (uc_stub_remote_add(st.device, st.type, st.instance, st.value) < 0)) {
				return bad(opt, val);
			}
		} else if (strcmp(opt, "--io") == 0) {
			char name[32];
			const char *eq = strchr(val, '=');
			double v;

			if ((eq == NULL) || ((size_t)(eq - val) >= sizeof(name)) ||
			    !parse_double(eq + 1, &end, &v) ||
			    ((*end != '\0') && (strcmp(end, ":out") != 0))) {
				return bad(opt, val);
			}
			memcpy(name, val, (size_t)(eq - val));
			name[eq - val] = '\0';
			if (uc_stub_io_add(name, v, *end != '\0') < 0) {
				return bad(opt, val);
			}
		} else if (strcmp(opt, "--at") == 0) {
			if ((sc->n_steps >= UC_SCENARIO_MAX_STEPS) || (parse_step(val, &st) < 0)) {
				return bad(opt, val);
			}
			sc->steps[sc->n_steps++] = st;
		} else if (strcmp(opt, "--run") == 0) {
			sc->run_ms = strtoull(val, &end, 10);
			if ((end == val) || (*end != '\0')) {
				return bad(opt, val);
			}
		} else if (strcmp(opt, "--heap") == 0) {
			if (!parse_u32(val, &end, &sc->heap) || (*end != '\0')) {
				return bad(opt, val);
			}
		} else if (strcmp(opt, "--stack") == 0) {
			if (!parse_u32(val, &end, &sc->stack) || (*end != '\0')) {
				return bad(opt, val);
			}
		} else if (strcmp(opt, "--pool") == 0) {
			if (!parse_u32(val, &end, &sc->pool) || (*end != '\0')) {
				return bad(opt, val);
			}
		} else {
			return bad("option", opt);
		}
	}
	/* stable order by time; equal times keep the command-line order */
	for (size_t i = 1; i < sc->n_steps; i++) {
		struct uc_step key = sc->steps[i];
		size_t j = i;

		while ((j > 0) && (sc->steps[j - 1].at_ms > key.at_ms)) {
			sc->steps[j] = sc->steps[j - 1];
			j--;
		}
		sc->steps[j] = key;
	}
	return 0;
}

static int32_t apply(const struct uc_step *st)
{
	switch (st->kind) {
	case STEP_REMOTE:
		return uc_stub_remote_set(st->device, st->type, st->instance, st->value, true);
	case STEP_SILENT:
		return uc_stub_remote_set(st->device, st->type, st->instance, st->value, false);
	case STEP_FAIL:
		return uc_stub_remote_fail(st->device, st->type, st->instance, (int32_t)st->value);
	case STEP_LOCAL:
		return uc_stub_obj_set_pv(st->type, st->instance, st->value);
	case STEP_WRITE:
		return uc_stub_client_write(st->type, st->instance, UC_PROP_PRESENT_VALUE,
					    st->value, st->priority);
	case STEP_RELINQUISH:
		return uc_stub_client_relinquish(st->type, st->instance, st->priority);
	case STEP_RESTART:
		uc_stub_stop();
		return uc_stub_start();
	}
	return -1;
}

int uc_scenario_run(const struct uc_scenario *sc, int32_t *start_rc)
{
	uint64_t t0;
	int32_t rc;

	rc = uc_stub_start();
	*start_rc = rc;
	if (rc != 0) {
		return 1;
	}
	t0 = uc_stub_now();
	for (size_t i = 0; i < sc->n_steps; i++) {
		const struct uc_step *st = &sc->steps[i];
		uint64_t at = t0 + st->at_ms;

		if (at > sc->run_ms + t0) {
			break;
		}
		if ((at > uc_stub_now()) && !uc_stub_run(at - uc_stub_now())) {
			return 1;
		}
		rc = apply(st);
		if (st->kind == STEP_RESTART) {
			*start_rc = rc;
			if (rc != 0) {
				return 1;
			}
		} else if (rc < 0) {
			fprintf(stderr, "step %zu at %llu ms failed (%d)\n", i,
				(unsigned long long)st->at_ms, (int)rc);
		}
	}
	if ((t0 + sc->run_ms > uc_stub_now()) && !uc_stub_run(t0 + sc->run_ms - uc_stub_now())) {
		return 1;
	}
	/* deliver what the last steps queued */
	if (!uc_stub_run(0)) {
		return 1;
	}
	return uc_stub_running() ? 0 : 1;
}

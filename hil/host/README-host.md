# HIL host operations: setup, CI and nightly runs

This directory holds everything the HIL host needs besides the `hilrig` package: host setup,
the GitHub Actions runner integration, the nightly trigger and the privileged entry points.
The design is `docs/HIL.md` §8.4, §8.5, §10 and §11 (W1, W3). Decisions it relies on:

- **D27**: CI runs only on the self-hosted HIL host. A push to the HIL branch builds and runs
  unit and SIL tests and never touches the bench. The nightly bench run starts from a host
  timer that force-pushes to the machine branch `hil/nightly`.
- **D28**: each app gets one Twister invocation with its own `--alt-config-root` and `-O`,
  identical for `--build-only` and `--test-only`.
- **D32**: both jobs run directly on the host as user `hil`, without a job container. That
  user's only sudo command is the root-owned `/opt/hil/bin/run-hil`.

## Files

| File | Installed as | Runs as | What |
|---|---|---|---|
| `bootstrap.sh` | (run from the checkout) | root | host setup: packages, SDK 1.0.1, udev, dumpcap, docker image, bacnet-stack tools, venv, west workspace, net wrappers |
| `install-runner.sh` | (run from the checkout) | root | user `hil`, entry points, the sudoers entry, tmpfiles, systemd units, runner hook, runner registration |
| `run-hil` | `/opt/hil/bin/run-hil` | root (sudo) | the hardware half of a run, or `--sil` |
| `pre-flash.sh` | `/opt/hil/bin/pre-flash.sh` | root (inside run-hil) | Twister `pre_script`: the R-07 OpenOCD guard before every DUT flash |
| `nightly.sh` | `/opt/hil/bin/nightly` | hil | pushes a run commit to `hil/nightly`; runs the fallback when no Actions run starts |
| `hil-nightly.service`, `hil-nightly.timer` | `/etc/systemd/system/` | hil | nightly at 01:17 host time |
| `job-started.sh` | `/opt/hil/hooks/job-started.sh` | hil | runner hook: tidies the job's own work tree under `timeout 60`; never touches the bench |
| `99-hil.rules` | `/etc/udev/rules.d/99-hil.rules` | udev | `/dev/hil/{dut0,stim0,rs485}` by USB serial, `ID_MM_DEVICE_IGNORE` |
| `map.yml.example` | `/etc/hil/bench1/map.yml` | Twister | hardware map: the DUT only, `pre_script`, fixture `hil_bench:<bench.yml>` |
| `bench.yml.example` | `/etc/hil/bench1/bench.yml` | hilrig | the bench description (commissioning values) |
| `github-workflow-hil.yml` | `.github/workflows/hil.yml` (see below) | Actions | build and hil jobs |
| `build.sh`, `install-net-wrappers.sh` | (from the checkout) | any / root | stand-alone image builds; the `hil-net-up`/`hil-net-down` wrappers |
| `isolated.sh` | (from the checkout) | root | runs one pytest session in private namespaces (`-d`: with its own dockerd for the key-log broker); for SIL runs next to another run (hil/README.md, "SIL against the firmware") |

`hil/twister/` holds the Twister side: `gen.py` renders the alt configs and
`ci-build.sh` is the build half of a run (see "A run, end to end").

Configuration lives in `/etc/hil/hil.env`: KEY=VALUE lines, root-owned, written by
`bootstrap.sh`. It gives the bench name, `/opt/hil/{out,ws,venv}`, the SDK path, the
bacnet-stack tools and the three branch names. `run-hil` parses it as data and only accepts
it root-owned and not group- or world-writable. The systemd service reads it as an
`EnvironmentFile`.

## 1. Set up a host (W1)

Ubuntu 24.04, x86-64. Work from a clone of this repository on the HIL branch:

```sh
sudo hil/host/bootstrap.sh                          # all steps; idempotent
sudo hil/host/bootstrap.sh --dry-run                # print what it would do
sudo hil/host/bootstrap.sh --steps udev --dut-sn <DUT ST-LINK SN> --stim-sn <STIM ST-LINK SN>
```

The steps, in order:

- **config**: user `hil`, group `hil-ops`, and `/etc/hil/hil.env`, `bench.yml` and `map.yml`.
- **packages**: the Zephyr build tools and the rig tools. It installs `dnsmasq-base`, not the
  `dnsmasq` service, and turns off the host's `mosquitto` service, because the rig runs its
  own servers inside netns `svc`.
- **sdk**: `zephyr-sdk-1.0.1_linux-x86_64_minimal`, checked against the release `sha256.sum`,
  then `setup.sh -t arm-zephyr-eabi -h -c`. `-h` installs OpenOCD at
  `<sdk>/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin`.
- **udev**: the SDK's `60-openocd.rules` plus `99-hil.rules` with the serials filled in.
- **dumpcap**: capabilities through `dpkg-reconfigure wireshark-common`.
- **docker**: dockerd enabled and `eclipse-mosquitto:2.1.2-alpine` pulled. Ubuntu ships
  mosquitto 2.0.18, which has no `--tls-keylog`.
- **bacnet**: bacnet-stack at the pinned 54544d02, built with `make BACDL=bip BBMD=full bip`
  (not `-j`), into `/opt/hil/tools/bacnet-stack/bin`.
- **venv**: `/opt/hil/venv`, Python 3.12, root-owned: west, hilrig and its dependencies,
  cbor2 and jsonschema for the BACnet SMP client, and bacpypes3.
- **workspace**: `/opt/hil/ws`, owned by `hil`. The manifest repository is a clone of this
  repository at `ws/BACNet-uc`, whose `west.yml` equals the firmware branches' (`survey.sh`
  checks that). The step runs `west update --narrow`, installs the Zephyr and MCUboot Python
  requirements, and installs `pytest-twister-harness` with `pip -e` from `$ZEPHYR_BASE`.
- **net**: `install-net-wrappers.sh` with `HIL_GROUP=hil-ops`. Add human operators to
  `hil-ops`. The runner user `hil` is kept out of it, so its only sudo rule is run-hil.

At commissioning (W1), read the UID with OpenOCD, compute the MAC and the MQTT client ID,
and fill in `/etc/hil/bench1/bench.yml` (see `bench.yml.example`). Then run the `udev` step
again with both ST-LINK serials and put the DUT serial into `map.yml` `id`. Before the
first run, check that `ls -l /dev/hil/` shows `dut0` and `stim0`.

Because pytest-twister-harness is installed in the venv, its pytest plugin loads in every
pytest session of that venv, and it needs `ZEPHYR_BASE` to be set. Twister calls always pass
`--allow-installed-plugin` (D2); for plain pytest, export
`ZEPHYR_BASE=/opt/hil/ws/zephyr` (or pass `-p no:twister_harness`).

## 2. Install the CI entry points and register the runner (W3)

```sh
sudo hil/host/install-runner.sh --nightly-repo https://github.com/<owner>/BACNet-uc.git
```

This installs the files in the table above. `/etc/sudoers.d/hil-run` has exactly one line,
`hil ALL=(root) NOPASSWD: /opt/hil/bin/run-hil`, and is checked with `visudo -c`. The script
also writes `/etc/tmpfiles.d/hil.conf`: `/run/hil` for the locks (group `hil`), and
`/opt/hil/out`, whose run directories are removed 7 days after their last change. Run it
again after every change to these files: root only ever runs the installed copies.

Register the runner as user `hil`, with the labels the workflow uses (`hil-build`, `hil`,
`bench1`):

1. In the repository's Settings → Actions → Runners → New self-hosted runner, copy the
   download commands and the registration token.
2. Unpack the runner into `/opt/hil/runner`.
3. Run `sudo RUNNER_TOKEN=<token> hil/host/install-runner.sh --register https://github.com/<owner>/BACNet-uc`.
   It runs `config.sh --unattended --labels hil-build,hil,bench1` as `hil`, installs the
   service with `svc.sh install hil`, and sets
   `ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/hil/hooks/job-started.sh` in the runner's `.env`.
   The token comes from the environment, never from argv.

In the repository settings, keep fork pull request workflows disabled, and add no secrets:
no job needs one (R19).

## 3. Install the workflow, once the runner is registered

The workflow ships here as `github-workflow-hil.yml`, not in `.github/workflows/`: with no
runner registered, every push would queue a run that never starts. Once `hil` shows as idle
under Settings → Actions → Runners:

```sh
git checkout claude/hardware-in-loop-testing-x74tww
mkdir -p .github/workflows
cp hil/host/github-workflow-hil.yml .github/workflows/hil.yml
git add .github/workflows/hil.yml && git commit -m "Add the HIL workflow" && git push
```

- Only the HIL branch and `hil/nightly` (which nightly.sh derives from it) may carry the
  file. The firmware branches never do, so their pushes trigger nothing. The default branch
  does not either, so `schedule` and `workflow_dispatch` stay inert (D27). Once the file
  reaches the default branch (open question 3: create `main`), dispatch works and the nightly
  can move to `on: schedule`.
- The first push that adds the file starts a build job. That is the "a push starts a run"
  check of §10 item 6.
- actionlint 1.7.12 rejects `concurrency.queue`. GitHub's workflow syntax documents
  `queue: max` (up to 100 pending runs; not combinable with `cancel-in-progress: true`), so
  that one finding is expected. With the key removed, actionlint and shellcheck report
  nothing.

## 4. Nightly runs

The timer starts `/opt/hil/bin/nightly` as `hil` at 01:17 host time. The script:

1. fetches its own clone `/var/lib/hil/nightly-repo`;
2. runs `git checkout -B hil/nightly origin/<HIL branch>`;
3. writes `.hil-run.env` with `BACNET_REF` and `MQTT_REF` (the branch tips as SHAs),
   `SELECT=not destructive`, `CYCLES=20` (Sundays: 50 and `WEEKLY=1`, which adds
   `--timeout-multiplier 2`) and `CREATED`;
4. commits that file and force-pushes `hil/nightly`.

Set up the push token once. Create a fine-grained token with contents write and actions read
on this repository only; it stays in the `hil` user's own gh login and never reaches a job:

```sh
sudo -u hil gh auth login --with-token < token.txt && sudo -u hil gh auth setup-git
sudo hil/host/install-runner.sh --enable-nightly
sudo -u hil /opt/hil/bin/nightly --select "rig" --cycles 5      # a manual run, same path
```

**Fallback.** Ten minutes after the push, the script checks `gh run list --branch
hil/nightly` for a run of its commit. If there is none (runner offline, Actions outage), it
runs both halves itself:

1. `hil/twister/ci-build.sh` on worktrees of the same three commits;
2. `sudo run-hil --local`, which also copies the results to `/var/lib/hil/nightly/<UTC date>`.

`--no-push` goes straight to the fallback; `--no-fallback` only pushes.

## 5. A run, end to end

**Build half** (`hil/twister/ci-build.sh -o OUT -f FW -m MQ`, user `hil`; the build job,
or the nightly fallback). It snapshots the sources, renders the alt configs, builds the
stimulus image and every scenario (`west twister --build-only`), then runs
`check_catalog.py` on each DUT build and SEC-01 on each release and variant build. It writes
the run directory `OUT = /opt/hil/out/<run id>-<attempt>`, outside `_work`:

| OUT/ | Content |
|---|---|
| `src/{hil,bacnet,mqtt}/`, `src/*.rev` | snapshots of the three checkouts, so the hil job, possibly on another runner, tests exactly what was built |
| `site/`, `alt/{bacnet,mqtt}/sample.yaml`, `twister/{bacnet,mqtt}.args` | `gen.py`: rendered site configs, alt configs, the common Twister arguments |
| `images/stim/` | the stimulus image (`hil/host/build.sh stim`) |
| `bacnet/`, `mqtt/` | Twister output (`-O`): builds and `twister.json` |
| `plain/{bacnet,mqtt}/` | plain app builds (`west build --cmake-only`, no site configuration): the SEC-01 reference |
| `results/build/` | `sizes.tsv` per scenario, `check_catalog.py` reports, SEC-01 JUnit (`sec01.xml`) |

The scenarios (docs/HIL.md §8.5) are:

- `hil.bacnet.release` and `hil.bacnet.instrumented` (`-S hil -S hil-io`);
- `hil.mqtt.release`, `hil.mqtt.release.mtls` and `hil.mqtt.instrumented` (`-S hil`);
- with `--it2`, also `hil.bacnet.release.mcuboot` (sysbuild) and `hil.mqtt.variant`.

Every scenario uses `harness: pytest`, `timeout: 3600`, fixture `hil_bench`,
`pytest_root: [$HIL_TESTS]` and session DUT scope. The images are built exactly like
`hil/host/build.sh`'s: the common arguments carry `--disable-warnings-as-errors`, because
Twister's default `CONFIG_COMPILER_WARNINGS_AS_ERRORS=y` would add a symbol that is not on
the D24 allowlist, and its `--edtlib-Werror` rejects the catalog binding's `uc` vendor
prefix.

**Hardware half** (`sudo run-hil [--select E] [--cycles N] [--weekly] OUT`, the hil job):

1. Take `flock -w 1800 /run/hil/bench1.lock`; exit 75 if it stays busy.
2. Turn every port of the bench hub on (`uhubctl -a on`) and run `stim safe`.
3. Check the stimulus firmware with OpenOCD `verify_image` against
   `OUT/images/stim/zephyr/zephyr.bin`, selecting the probe by serial. The core is halted by
   a reset and then released. If the check fails, reflash with
   `west flash -r openocd -- --cmd-pre-init "adapter serial <SN>"`, check again, and run
   `stim safe` again.
4. For each app, run `west twister` with the `OUT/twister/<app>.args` lines, plus
   `--test-only --pytest-args=--cycles=N [--pytest-args=--hil-select=E]`. run-hil accepts
   the args file only in the layout gen.py writes, with this bench's `map.yml` and `-O`
   inside OUT.
   - Twister starts pytest on `OUT/src/hil/hil/tests`, as root. The conftest detects
     Twister mode, takes the bench from the `hil_bench` fixture, runs the guard and requests
     `dut`.
   - The harness runs `pre-flash.sh` (the guard again), then flashes with
     `hla_serial <id>`.
5. Fail the run when any scenario did not pass. Twister reports a scenario whose pytest
   selected no test as *skipped* and exits 0; run-hil counts that as a failure (R31).
6. Collect `twister.json`, the JUnit files (`twister_report.xml`, per-scenario `report.xml`),
   `handler.log`, `device.log`, `twister_harness.log` and the pytest artifacts (`--artifacts`
   = `OUT/results/<app>/<scenario>`: pcaps with keys, stim.log, service logs, rig reports)
   under `OUT/results/`, owned by the calling user. Then run `stim safe` once more.

`sudo run-hil --dry-run OUT` prints the plan without running anything.
`sudo run-hil --sil OUT` runs `pytest --sil` on the snapshot as root, under the same lock,
because SIL and the bench share netns `lan-a`. It does no bench actions. This is the build
job's SIL step.

## 6. Local runs and the bench lock

Hold the bench lock for any manual work on the bench. CI then waits up to 30 minutes and
gives up with exit status 75, so local work always wins:

```sh
sudo flock /run/hil/bench1.lock -c 'ZEPHYR_BASE=/opt/hil/ws/zephyr /opt/hil/venv/bin/pytest \
    --hil --bench /etc/hil/bench1/bench.yml -m "rig or release" hil/tests'
```

List the scenarios without building (the W3 check):

```sh
WEST_WS=/opt/hil/ws hil/twister/ci-build.sh -o /tmp/list -f <bacnet checkout> -m <mqtt checkout> --list
# which runs, per app, with the rendered alt root:
west twister --list-tests -T <bacnet checkout>/firmware --alt-config-root /tmp/list/alt/bacnet --allow-installed-plugin
```

## 7. Security notes (R19)

- The bench jobs run as root through run-hil, and the tests that run are the pushed ones.
  Anyone who can push to the HIL branch can therefore run code as root on this host. The
  mitigations are:
  - a private repository with owner-only pushes;
  - no fork workflows and no secrets in jobs;
  - the push token only in the `hil` user's gh login;
  - `lan-a` never bridged to the uplink.
- run-hil, pre-flash.sh, nightly and the hook are installed root-owned. sudo resets the
  environment, and run-hil builds its own environment from `/etc/hil/hil.env`. It accepts
  run directories only directly under `/opt/hil/out`, and args files only in the gen.py
  layout.
- The workspace `/opt/hil/ws` belongs to `hil`, because the build job updates it, and root
  runs Twister from it. That is the same trust boundary as above.

## Known gaps

- `tests/unit/test_conftest_rules.py` starts pytest subprocesses with an environment of its
  own, without `ZEPHYR_BASE`. In the host venv, where pytest-twister-harness is installed,
  that plugin then aborts those subprocesses ("ZEPHYR_BASE environment variable is not set"),
  so the eight tests fail under `run-hil --sil` until they pass `-p no:twister_harness`.
- `hil/.gitignore` contains `tools/`, which also ignores `hil/tools/` (`survey.sh`,
  `check_catalog.py`). Until it is narrowed to `tools/bacnet-stack/`, a CI checkout lacks
  those files, and ci-build.sh warns that the catalog was not checked.
- Registering the runner, the nightly push and the checks from inside a real job (§10 item 6)
  need the host, the token and the boards. They were not done here.

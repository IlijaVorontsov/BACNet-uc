#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""validate.py - run the BACnet-uc example modules in WAMR and compare them
with native builds of the same sources.

For every scenario (SCENARIOS below):
  1. uc-wamr-runner runs the .wasm (fast interpreter, stack 4 KiB, app heap
     8 KiB, like the apps.json defaults) and reports the linear memory WAMR
     allocated; it must equal the size predicted from the module
     (sdk/tools/wasmfile.py: __heap_base + heap) and WAMR's bounds.
  2. The final state dump (objects, priority arrays, remote points, kv,
     log lines, counters; before and after the stop) of the WebAssembly run
     must be identical to the dump of the native scenario runner
     (build/host/scenario_<app>), and so must the dump of the x86-64 AOT
     file when build/aot/native_sim_native_64/<app>.aot exists.
  3. With --iwasm: WAMR's iwasm loads every module (and AOT file) with the
     natives from libuc_bacnet_stub.so and calls uc_app_api_version.
  4. A bounds probe documents WAMR's linear memory rounding (see README).
Exit status 1 when a check fails.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK / "tools"))

import uc_check  # noqa: E402

HEAP = 8192
STACK = 4096

# (scenario name, module, runner arguments)
SCENARIOS: list[tuple[str, str, list[str]]] = [
    ("blinky", "blinky", ["--run", "5000"]),
    ("blinky-channel", "blinky", ["--io", "do0=0:out", "-p", "channel=do0", "-p", "period_ms=250",
                                  "--perms", "io", "--run", "3000"]),
    ("blinky-output", "blinky", ["--obj", "4:1=0:LED", "-p", "type=4", "-p", "priority=8",
                                 "--run", "4000"]),
    ("thermostat-local", "thermostat", [
        "--obj", "0:1=19.5:Room", "--obj", "1:1=0:Valve", "-p", "setpoint=21.5",
        "--at", "5000:local:0:1=20.5", "--at", "8000:write:2:1=23@8",
        "--at", "15000:relinquish:2:1@8", "--at", "20000:write:2:1=99@8", "--run", "70000"]),
    ("thermostat-remote-pwm", "thermostat", [
        "-p", "sensor_device=1001", "-p", "out_type=4", "-p", "pwm_ms=10000", "-p", "ti_s=0",
        "-p", "poll_ms=3000", "--remote", "1001:0:1=19.25", "--obj", "4:1=0:Relay",
        "--at", "20000:silent:1001:0:1=20", "--at", "40000:fail:1001:0:1=-4",
        "--at", "130000:fail:1001:0:1=0", "--run", "150000"]),
    ("thermostat-kv-restart", "thermostat", [
        "--obj", "0:1=20:Room", "--obj", "1:1=0:Valve", "--at", "2000:write:2:1=24@16",
        "--at", "5000:restart", "--run", "8000"]),
    ("alarm", "alarm", [
        "--obj", "0:1=25:Room", "-p", "count_instance=11", "-p", "delay_ms=1500",
        "--at", "2000:local:0:1=31", "--at", "6000:local:0:1=29.5",
        "--at", "7000:local:0:1=28", "--at", "9000:local:0:1=35", "--at", "12000:restart",
        "--at", "14000:write:2:11=0@8", "--at", "15000:write:5:10=0@8", "--run", "18000"]),
    ("alarm-remote-low", "alarm", [
        "-p", "src_device=1001", "-p", "src_instance=4", "-p", "direction=low",
        "-p", "threshold=5", "-p",
        "poll_ms=1000", "--perms", "bacnet.local,bacnet.remote", "--remote", "1001:0:4=8",
        "--at", "3000:remote:1001:0:4=4.5", "--at", "6000:silent:1001:0:4=6",
        "--at", "9000:fail:1001:0:4=-12", "--run", "80000"]),
    ("uc-link", "uc-link", [
        "-p", "count=4", "-p", "l0=1001 0 1 2 10 cov 1000 0 2 1",
        "-p", "l1=1001 0 2 1 1 poll 500 8 1 0", "-p", "l2=1001 0 1 2 12 cov 1000 0 1",
        "-p", "l3=1000 3 1 5 13 cov 0 0 1 0", "--obj", "1:1=0:Valve", "--obj", "3:1=0:Button",
        "--remote", "1001:0:1=20.5", "--remote", "1001:0:2=3",
        "--at", "3000:remote:1001:0:1=21", "--at", "4000:silent:1001:0:2=4",
        "--at", "5000:local:3:1=1", "--at", "6000:fail:1001:0:2=-4", "--run", "10000"]),
    ("uc-link-bad-count", "uc-link", ["-p", "count=9", "--run", "1000"]),
]
EXPECT_START_FAIL = {"uc-link-bad-count"}


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)


def first_diff(a: str, b: str) -> str:
    for i, (x, y) in enumerate(zip(a.splitlines(), b.splitlines(), strict=False)):
        if x != y:
            return f"line {i + 1}:\n      wasm:   {x}\n      native: {y}"
    return "length differs"


def bounds_probe(runner: str, uc_cc: str, tmp: Path) -> str:
    """Write just behind the allocated linear memory of a module whose size
    is not page aligned; WAMR 2.4.5 accepts it (bounds rounded to 4 KiB)."""
    src = tmp / "bounds_probe.c"
    src.write_text(
        "#include <bacnet_uc.h>\nUC_APP_DECLARE()\n"
        "UC_EXPORT(uc_app_init) int32_t uc_app_init(void)\n{\n"
        "\tvolatile uint8_t *p = (volatile uint8_t *)(uintptr_t)uc_param_num(\"addr\", 0);\n"
        "\t*p = 0x5a;\n\treturn *p == 0x5a ? 0 : 1;\n}\n", encoding="utf-8")
    wasm = tmp / "bounds_probe.wasm"
    res = run([uc_cc, "-q", "--no-page-align", "-o", str(wasm), str(src)])
    if res.returncode != 0:
        return f"probe build failed: {res.stderr.strip()}"
    info = uc_check.check_file(str(wasm), heap_bytes=HEAP).info
    linear, bounds = info["linear_memory_bytes"], info["linear_memory_bounds_bytes"]
    results = []
    for addr in (linear, bounds - 1, bounds):
        r = run([runner, str(wasm), "--json", "-p", f"addr={addr}", "--run", "0"])
        trapped = '"trapped": true' in r.stdout
        results.append(f"{addr}: {'trap' if trapped else 'accepted'}")
    return (f"module memory {linear} B (not page aligned), WAMR bounds {bounds} B; "
            f"write at {', '.join(results)}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--build", default=str(SDK.parent / "build"), help="wasm/build directory")
    p.add_argument("--runner", default="/tmp/uc-wamr-runner/uc-wamr-runner")
    p.add_argument("--iwasm", help="iwasm built by build.sh --iwasm")
    p.add_argument("--stub-lib", help="libuc_bacnet_stub.so (default: next to the runner)")
    p.add_argument("--json", help="write the results as JSON to this file")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    build = Path(args.build)
    aot_dir = build / "aot" / "native_sim_native_64"
    failures: list[str] = []
    rows: list[dict] = []

    print(f"{'scenario':24} {'module B':>8} {'memory':>7} {'bounds':>7} {'pool mod':>8} "
          f"{'inst':>5} {'env':>5} {'ticks':>5} {'events':>6}  wasm=native  aot=native")
    for name, module, sargs in SCENARIOS:
        wasm = build / f"{module}.wasm"
        native = build / "host" / f"scenario_{module}"
        rep = uc_check.check_file(str(wasm), heap_bytes=HEAP)
        predicted = rep.info.get("linear_memory_bytes")
        base = [args.runner, str(wasm), "--heap", str(HEAP), "--stack", str(STACK), *sargs]
        r = run(base + ["--json"])
        try:
            res = json.loads(r.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            failures.append(f"{name}: runner failed: {r.stderr.strip()}")
            continue
        expect_ok = name not in EXPECT_START_FAIL
        if (res["result"] == 0) != expect_ok or res["trapped"]:
            failures.append(f"{name}: result {res['result']} start {res['start_rc']} "
                            f"trap '{res['trap']}' {r.stderr.strip()}")
        if res["linear_memory_bytes"] != predicted:
            failures.append(f"{name}: linear memory {res['linear_memory_bytes']} B, predicted "
                            f"{predicted} B")
        if res["bounds_bytes"] != res["linear_memory_bytes"]:
            failures.append(f"{name}: WAMR bounds {res['bounds_bytes']} B != allocated "
                            f"{res['linear_memory_bytes']} B")

        dump_w = run(base + ["--dump"]).stdout
        dump_n = run([str(native), *sargs, "--dump"]).stdout
        same = dump_w == dump_n and bool(dump_n)
        if not same:
            failures.append(f"{name}: wasm and native dumps differ at {first_diff(dump_w, dump_n)}")
        aot = aot_dir / f"{module}.aot"
        same_aot = "-"
        if aot.is_file():
            dump_a = run([args.runner, str(aot), "--heap", str(HEAP), "--stack", str(STACK),
                          *sargs, "--dump"]).stdout
            same_aot = "yes" if dump_a == dump_n else "NO"
            if dump_a != dump_n:
                failures.append(f"{name}: AOT and native dumps differ at "
                                f"{first_diff(dump_a, dump_n)}")
        if args.verbose:
            print(dump_w)
        print(f"{name:24} {res['file_bytes']:8} {res['linear_memory_bytes']:7} "
              f"{res['bounds_bytes']:7} {res['pool_module']:8} {res['pool_instance']:5} "
              f"{res['pool_exec_env']:5} {res['ticks']:5} {res['events']:6}  "
              f"{'yes' if same else 'NO':11}  {same_aot}")
        rows.append({"scenario": name, "module": module, **res, "predicted": predicted,
                     "wasm_equals_native": same, "aot_equals_native": same_aot})

    if args.iwasm:
        lib = args.stub_lib or str(Path(args.runner).parent / "libuc_bacnet_stub.so")
        mods = sorted(build.glob("*.wasm")) + sorted(aot_dir.glob("*.aot"))
        for m in mods:
            r = run([args.iwasm, f"--native-lib={lib}", f"--heap-size={HEAP}",
                     f"--stack-size={STACK}", "-f", "uc_app_api_version", str(m)])
            ok = "0x10000:i32" in r.stdout
            print(f"iwasm {m.relative_to(build)}: {'ok' if ok else 'FAILED'}")
            if not ok:
                failures.append(f"iwasm {m}: {r.stdout.strip()} {r.stderr.strip()}")

    with tempfile.TemporaryDirectory(prefix="uc-probe-") as tmp:
        print("bounds probe:", bounds_probe(args.runner, str(SDK / "uc-cc"), Path(tmp)))

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    for f in failures:
        print("FAIL", f)
    print(f"{len(SCENARIOS)} scenarios, {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

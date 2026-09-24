#!/usr/bin/env python3
"""Run test-vector files against the driver binary (test infrastructure).

Usage:
    python3 run_vectors.py [--drv ./drv] [--verbose] [--json OUT] FILE.vec [FILE.vec ...]

Vector file format (plain text):

    ### <block-id> [tag ...]
    <input line fed to the driver>
    <input line ...>
    ---
    <expected output line>
    <expected output line ...>

Every input line produces exactly one output line.  A block passes when every
output line matches the expected line exactly (trailing whitespace ignored).
Lines outside blocks that start with '#' (but not '###') are comments.
Exit status is 0 when every block passes.
"""
import argparse
import json
import os
import subprocess
import sys


def parse_vec(path):
    blocks = []
    cur = None
    mode = None
    with open(path) as f:
        for raw in f:
            line = raw.rstrip("\n").rstrip()
            if line.startswith("###"):
                parts = line[3:].split()
                if not parts:
                    raise SystemExit(f"{path}: block header without id")
                cur = {"id": parts[0], "tags": parts[1:], "inp": [], "exp": [], "file": path}
                blocks.append(cur)
                mode = "inp"
                continue
            if cur is None:
                continue
            if line == "---" and mode == "inp":
                mode = "exp"
                continue
            if line == "" or (line.startswith("#") and mode == "inp"):
                continue
            cur[mode].append(line)
    return blocks


def _run_batch(drv, blocks, timeout):
    """Run blocks in one driver process. Returns list of outputs (None for blocks
    that were not completed) and the index of the block that crashed/hung, if any."""
    stdin = []
    for i, b in enumerate(blocks):
        stdin.append(f"@block {i}")
        stdin.extend(b["inp"])
    data = ("\n".join(stdin) + "\n").encode()
    status = "ok"
    try:
        p = subprocess.run([drv], input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        out = p.stdout
        if p.returncode != 0:
            status = f"exit{p.returncode}"
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        status = "timeout"
    lines = out.decode("utf-8", "replace").split("\n")
    res = [None] * len(blocks)
    cur_idx = None
    cur_lines = []
    for ln in lines:
        if ln.startswith("@block "):
            if cur_idx is not None:
                res[cur_idx] = cur_lines
            try:
                cur_idx = int(ln.split()[1])
            except (IndexError, ValueError):
                cur_idx = None
            cur_lines = []
        elif cur_idx is not None:
            cur_lines.append(ln.rstrip())
    last = cur_idx
    if cur_idx is not None:
        res[cur_idx] = cur_lines
    if status != "ok":
        # the last started block is unreliable (crash/hang happened inside it)
        if last is not None:
            res[last] = None
        return res, status, (last if last is not None else 0)
    return res, status, None


def run_blocks(drv, blocks, timeout=60):
    """Returns list of dicts {id, ok, got, status}."""
    results = [None] * len(blocks)
    start = 0
    while start < len(blocks):
        chunk = blocks[start:]
        res, status, bad = _run_batch(drv, chunk, timeout)
        if bad is None:
            for i, r in enumerate(res):
                results[start + i] = (r if r is not None else [], "ok" if r is not None else "missing")
            break
        for i in range(bad):
            r = res[i]
            results[start + i] = (r if r is not None else [], "ok" if r is not None else "missing")
        results[start + bad] = ([], status)
        start = start + bad + 1
    out = []
    for b, (got, status) in zip(blocks, results):
        got = [g for g in got if g != ""] if status == "ok" else got
        ok = status == "ok" and got == b["exp"]
        out.append({"id": b["id"], "tags": b["tags"], "ok": ok, "got": got, "exp": b["exp"], "status": status})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drv", default="./drv")
    ap.add_argument("--json")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    drv = a.drv if os.path.sep in a.drv else "./" + a.drv
    if not os.path.exists(drv):
        print(f"driver {drv} not found - run make first", file=sys.stderr)
        return 2
    total = fails = 0
    allres = []
    for fn in a.files:
        blocks = parse_vec(fn)
        res = run_blocks(drv, blocks)
        for r in res:
            total += 1
            if not r["ok"]:
                fails += 1
                if a.verbose or fails <= 25:
                    print(f"FAIL {fn}:{r['id']} [{r['status']}]")
                    for e, g in zip(r["exp"] + [""] * 50, r["got"] + [""] * 50):
                        if e == "" and g == "":
                            break
                        mark = "  " if e == g else "!!"
                        print(f"   {mark} expected: {e!r:50} got: {g!r}")
        allres.extend(res)
    if fails > 25 and not a.verbose:
        print(f"... {fails - 25} more failures (use -v)")
    print(f"{total - fails}/{total} passed")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(allres, f)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

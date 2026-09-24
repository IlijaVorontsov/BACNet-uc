#!/usr/bin/env python3
"""Stage 1 analysis: conformance, divergence, self-test validity.

usage: analyze_s1.py WSROOT OUT.json
"""
import glob
import itertools
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from run_vectors import parse_vec, run_blocks  # noqa: E402
from lab import build, summarize, acceptance_for  # noqa: E402

IMPL = {"p": "bacapp.c", "x": "alarm.c"}


def main():
    root, outp = sys.argv[1], sys.argv[2]
    out = {}
    tmp = tempfile.mkdtemp()
    for d in ("p", "x"):
        oracle = parse_vec(os.path.join(LAB, d, "oracle", "v1.0.vec"))
        acc = parse_vec(acceptance_for(d, "1.0"))
        ref_ws = tempfile.mkdtemp(dir=tmp)
        shutil.copy(os.path.join(LAB, d, "ref", "v1.0", IMPL[d]), ref_ws)
        ref_exe, _ = build(d, "1.0", ref_ws, tmp)
        runs = {}
        for ws in sorted(glob.glob(os.path.join(root, "s1", d, "*", "*"))):
            arm, rep = ws.split(os.sep)[-2:]
            name = f"{arm}-{rep}"
            exe, how = build(d, "1.0", ws, tmp)
            if exe is None:
                runs[name] = {"arm": arm, "build": how}
                continue
            res = run_blocks(exe, oracle)
            ares = run_blocks(exe, acc)
            by_rule, by_cat = summarize(d, res)
            r = {"arm": arm, "build": how, "passed": sum(x["ok"] for x in res), "total": len(res),
                 "acceptance": [sum(x["ok"] for x in ares), len(acc)], "by_rule": by_rule, "by_cat": by_cat,
                 "got": {x["id"]: x["got"] if x["status"] == "ok" else ["<" + x["status"] + ">"] for x in res},
                 "lines": sum(1 for _ in open(os.path.join(ws, IMPL[d])))}
            # own tests (arm ii)
            tv = os.path.join(ws, "tests.vec")
            if os.path.exists(tv):
                try:
                    tests = parse_vec(tv)
                except SystemExit:
                    tests = []
                own = run_blocks(exe, tests)
                onref = run_blocks(ref_exe, tests)
                r["self_tests"] = {"blocks": len(tests), "pass_own": sum(x["ok"] for x in own),
                                   "agree_with_intent": sum(x["ok"] for x in onref),
                                   "wrong_blocks": [x["id"] for x in onref if not x["ok"]][:50]}
            runs[name] = r
        # pairwise divergence over all oracle inputs
        names = [n for n in runs if "got" in runs[n]]
        pair = {}
        for a, b in itertools.combinations(names, 2):
            ga, gb = runs[a]["got"], runs[b]["got"]
            diff = sum(1 for k in ga if ga[k] != gb.get(k))
            pair[f"{a}|{b}"] = diff / len(ga)
        # per block: number of distinct behaviours among implementations
        distinct = {}
        for blk in oracle:
            outs = {json.dumps(runs[n]["got"][blk["id"]]) for n in names}
            distinct[blk["id"]] = len(outs)
        for n in names:
            del runs[n]["got"]
        out[d] = {"runs": runs, "pairwise_diff": pair, "distinct_per_block": distinct}
    shutil.rmtree(tmp, ignore_errors=True)
    json.dump(out, open(outp, "w"), indent=1)
    # short report
    for d in out:
        print(f"== {d}")
        for n, r in out[d]["runs"].items():
            if "passed" not in r:
                print(f"  {n}: {r['build']}")
                continue
            st = r.get("self_tests")
            sts = f" self-tests {st['agree_with_intent']}/{st['blocks']} agree with intent" if st else ""
            print(f"  {n}: {r['passed']}/{r['total']} acc {r['acceptance']} {r['by_cat']}{sts}")
        pd = out[d]["pairwise_diff"]
        if pd:
            vals = sorted(pd.values())
            print(f"  pairwise diff: min {vals[0]:.3f} median {vals[len(vals)//2]:.3f} max {vals[-1]:.3f}")


if __name__ == "__main__":
    main()

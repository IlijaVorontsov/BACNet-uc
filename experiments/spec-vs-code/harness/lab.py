#!/usr/bin/env python3
"""Experiment tooling: workspace creation, scoring, divergence.

  lab.py mkws  --domain p|x --ver 1.0 --out DIR [--impl FILE] [--spec] [--tests FILE] [--cr N]
  lab.py score --domain p|x --ver 1.0 --ws DIR [--json OUT]
  lab.py split --domain p|x                      (visible unit-test subset)
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from run_vectors import parse_vec, run_blocks  # noqa: E402

IMPL = {"p": "bacapp.c", "x": "alarm.c"}
HDR = {"p": "bacapp.h", "x": "alarm.h"}
# oracle file used to score each version
ORACLE = {("p", "1.0"): "v1.0", ("p", "1.1"): "v1.1", ("p", "1.2"): "v1.1", ("p", "1.3"): "v1.3",
          ("x", "1.0"): "v1.0", ("x", "1.1"): "v1.1", ("x", "1.2"): "v1.2", ("x", "1.3"): "v1.3"}


def dom(d):
    # scratch layout: LAB/p ; repository layout: LAB/domains/p-bacapp
    flat = os.path.join(LAB, d)
    return flat if os.path.isdir(flat) else os.path.join(LAB, "domains", f"{d}-{IMPL[d][:-2]}")


def write_vec(blocks, path):
    with open(path, "w") as f:
        for b in blocks:
            f.write(f"### {b['id']} {' '.join(b['tags'])}\n")
            for ln in b["inp"]:
                f.write(ln + "\n")
            f.write("---\n")
            for ln in b["exp"]:
                f.write(ln + "\n")


def acceptance_for(d, ver):
    if d == "x" and ver == "1.3":
        return os.path.join(dom(d), "acceptance_v1.3.vec")
    return os.path.join(dom(d), "acceptance.vec")


def mkws(a):
    d, ver, out = a.domain, a.ver, a.out
    os.makedirs(out, exist_ok=True)
    pub = os.path.join(dom(d), "public", "v" + ver)
    for fn in os.listdir(pub):
        if fn == HDR[d] and a.keep_header and os.path.exists(os.path.join(out, fn)):
            continue
        shutil.copy(os.path.join(pub, fn), os.path.join(out, fn))
    shutil.copy(os.path.join(HERE, "run_vectors.py"), out)
    shutil.copy(acceptance_for(d, ver), os.path.join(out, "acceptance.vec"))
    if a.impl:
        shutil.copy(a.impl, os.path.join(out, IMPL[d]))
    if a.spec:
        shutil.copy(os.path.join(dom(d), "SPEC.md"), out)
    if a.tests:
        os.makedirs(os.path.join(out, "tests"), exist_ok=True)
        shutil.copy(a.tests, os.path.join(out, "tests", "unit.vec"))
    if a.cr:
        shutil.copy(os.path.join(dom(d), "crs", f"CR{a.cr}.md"), os.path.join(out, "CR.md"))
    for junk in ("drv",):
        p = os.path.join(out, junk)
        if os.path.exists(p):
            os.remove(p)


def build(d, ver, ws, tmp, sanitize=False):
    """Build the workspace implementation with the pristine driver of `ver`.
    Uses the workspace header if it compiles, else the pristine header."""
    pub = os.path.join(dom(d), "public", "v" + ver)
    src = os.path.join(ws, IMPL[d])
    if not os.path.exists(src):
        return None, "no implementation"
    flags = ["-std=c11", "-O2", "-w"]
    if sanitize:
        flags = ["-std=c11", "-O1", "-g", "-w", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
    for hdr_src in (os.path.join(ws, HDR[d]), os.path.join(pub, HDR[d])):
        if not os.path.exists(hdr_src):
            continue
        bd = tempfile.mkdtemp(dir=tmp)
        shutil.copy(os.path.join(pub, "driver.c"), bd)
        shutil.copy(hdr_src, os.path.join(bd, HDR[d]))
        shutil.copy(src, os.path.join(bd, IMPL[d]))
        # extra local headers the implementation may include
        for fn in os.listdir(ws):
            if fn.endswith(".h") and fn != HDR[d]:
                shutil.copy(os.path.join(ws, fn), bd)
        exe = os.path.join(bd, "drv")
        r = subprocess.run(["cc"] + flags + ["-o", exe, os.path.join(bd, "driver.c"), os.path.join(bd, IMPL[d]), "-lm"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return exe, ("ws-header" if hdr_src.startswith(ws) else "pristine-header")
    return None, "compile error"


def rule_category(d):
    mod = __import__("gen_p" if d == "p" else "gen_x")
    return mod.RULES


def summarize(d, res):
    cats = rule_category(d)
    by_rule = {}
    by_cat = {}
    for r in res:
        rules = [t for t in r["tags"] if t in cats]
        primary = rules[0] if rules else "?"
        for t in rules:
            s = by_rule.setdefault(t, [0, 0])
            s[0] += r["ok"]
            s[1] += 1
        c = cats.get(primary, "?")
        s = by_cat.setdefault(c, [0, 0])
        s[0] += r["ok"]
        s[1] += 1
    return by_rule, by_cat


def score(a):
    d, ver, ws = a.domain, a.ver, a.ws
    tmp = tempfile.mkdtemp()
    try:
        exe, how = build(d, ver, ws, tmp)
        oracle = os.path.join(dom(d), "oracle", ORACLE[(d, ver)] + ".vec")
        blocks = parse_vec(oracle)
        if exe is None:
            res = [{"id": b["id"], "tags": b["tags"], "ok": False, "status": how} for b in blocks]
        else:
            res = run_blocks(exe, blocks)
        acc = parse_vec(acceptance_for(d, ver))
        acc_res = run_blocks(exe, acc) if exe else []
        by_rule, by_cat = summarize(d, res)
        out = {
            "domain": d, "ver": ver, "ws": ws, "build": how,
            "passed": sum(r["ok"] for r in res), "total": len(res),
            "acceptance": [sum(r["ok"] for r in acc_res), len(acc)],
            "by_rule": by_rule, "by_cat": by_cat,
            "fails": [r["id"] for r in res if not r["ok"]],
        }
        if a.dump:
            out["got"] = {r["id"]: r.get("got", []) for r in res}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f)
    print(json.dumps({k: out[k] for k in ("build", "passed", "total", "acceptance", "by_cat")}))


def split(a):
    """Stratified 50% visible unit-test subset of the v1.0 oracle (no FUZZ/RAND blocks)."""
    d = a.domain
    blocks = parse_vec(os.path.join(dom(d), "oracle", "v1.0.vec"))
    cats = rule_category(d)
    rnd = random.Random(99)
    groups = {}
    for b in blocks:
        if "FUZZ" in b["tags"] or "RAND" in b["tags"]:
            continue
        key = tuple(sorted(t for t in b["tags"] if t in cats))
        groups.setdefault(key, []).append(b)
    visible = []
    for key in sorted(groups):
        g = groups[key]
        rnd.shuffle(g)
        visible += g[: max(1, (len(g) + 1) // 2)]
    visible.sort(key=lambda b: b["id"])
    out = os.path.join(dom(d), "oracle", "visible_unit.vec")
    write_vec(visible, out)
    print(f"{out}: {len(visible)} of {len(blocks)} blocks visible")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd")
    m = sp.add_parser("mkws")
    m.add_argument("--domain", required=True)
    m.add_argument("--ver", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--impl")
    m.add_argument("--spec", action="store_true")
    m.add_argument("--tests")
    m.add_argument("--cr")
    m.add_argument("--keep-header", action="store_true")
    s = sp.add_parser("score")
    s.add_argument("--domain", required=True)
    s.add_argument("--ver", required=True)
    s.add_argument("--ws", required=True)
    s.add_argument("--json")
    s.add_argument("--dump", action="store_true")
    t = sp.add_parser("split")
    t.add_argument("--domain", required=True)
    a = ap.parse_args()
    {"mkws": mkws, "score": score, "split": split}[a.cmd](a)


if __name__ == "__main__":
    main()

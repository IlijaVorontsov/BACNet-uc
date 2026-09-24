#!/usr/bin/env python3
"""Mutation analysis: which test suites detect which single-point faults.

usage: mutate.py DOMAIN(p|x) OUT.json SUITE_NAME=FILE.vec [...]

Mutants are generated from the v1.0 reference implementation with classic operators
(relational/arithmetic/logical operator replacement, constant +-1, boolean flip,
negation removal, guard deletion).  Every mutant is compiled with the pristine driver
and run against all suites in one driver process.  A suite 'kills' a mutant when at
least one of its blocks fails (or the build/run crashes/hangs).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from run_vectors import parse_vec, run_blocks  # noqa: E402

IMPL = {"p": "bacapp.c", "x": "alarm.c"}
HDR = {"p": "bacapp.h", "x": "alarm.h"}

REL = [("<=", "<"), ("<", "<="), (">=", ">"), (">", ">="), ("==", "!="), ("!=", "==")]
ARITH = [("+", "-"), ("-", "+")]
LOGIC = [("&&", "||"), ("||", "&&")]


def code_mask(src):
    """True for characters that are code (not comment/string/char literal/preprocessor)."""
    mask = [True] * len(src)
    i = 0
    n = len(src)
    line_start = True
    while i < n:
        c = src[i]
        if line_start and c == "#":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                mask[k] = False
            i = j
            continue
        if c == "\n":
            line_start = True
            i += 1
            continue
        if not c.isspace():
            line_start = False
        if src.startswith("/*", i):
            j = src.find("*/", i + 2) + 2
            for k in range(i, j):
                mask[k] = False
            i = j
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            for k in range(i, j):
                mask[k] = False
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while src[j] != c:
                j += 2 if src[j] == "\\" else 1
            for k in range(i, j + 1):
                mask[k] = False
            i = j + 1
            continue
        i += 1
    return mask


def mutants(src):
    mask = code_mask(src)
    out = []
    seen = set()

    def add(i, j, new, op):
        m = src[:i] + new + src[j:]
        if m in seen:
            return
        seen.add(m)
        line = src.count("\n", 0, i) + 1
        out.append({"op": op, "line": line, "orig": src[i:j], "new": new, "src": m})

    # operators (tokenised roughly)
    for m in re.finditer(r"<<=|>>=|<=|>=|==|!=|&&|\|\||<<|>>|->|\+\+|--|\+=|-=|[<>+\-!]", src):
        i, j, tok = m.start(), m.end(), m.group()
        if not mask[i]:
            continue
        for a, b in REL:
            if tok == a:
                add(i, j, b, "ROR")
        for a, b in LOGIC:
            if tok == a:
                add(i, j, b, "LOR")
        if tok in ("+", "-"):
            # skip unary minus in literals like -1 after 'return' or '(' or ','
            prev = src[:i].rstrip()[-1:] if src[:i].rstrip() else ""
            if prev and (prev.isalnum() or prev in ")]_"):
                add(i, j, "-" if tok == "+" else "+", "AOR")
        if tok == "!" and src[j:j + 1] != "=":
            add(i, j, "", "NEG")
    # integer constants +-1
    for m in re.finditer(r"\b(0x[0-9A-Fa-f]+|\d+)(u|U|ULL|ull|u?l{0,2})?\b", src):
        i, j = m.start(1), m.end(1)
        if not mask[i]:
            continue
        # skip array sizes in declarations / static assert / includes
        ctx = src[max(0, i - 40):i]
        if "storage[" in ctx or "_Static_assert" in ctx:
            continue
        v = int(m.group(1), 0)
        for nv in (v + 1, v - 1):
            if nv < 0:
                continue
            s = hex(nv) if m.group(1).lower().startswith("0x") else str(nv)
            add(i, j, s, "CRP")
    # booleans
    for m in re.finditer(r"\b(true|false)\b", src):
        if mask[m.start()]:
            add(m.start(), m.end(), "false" if m.group() == "true" else "true", "BOOL")
    # guard deletion: 'return -1;' / 'return false;' / 'return 0;' / 'return;' inside if-bodies -> ';'
    for m in re.finditer(r"return (-1|false|0|NONE)?;", src):
        if mask[m.start()]:
            add(m.start(), m.end(), ";", "SDL")
    # statement deletion: single-line assignment / call statements
    for m in re.finditer(r"(?m)^[ \t]+((?:\w+(?:->|\.)?)+(?:\[[^\]\n]*\])?(?:\.\w+|->\w+)*\s*(?:[|&+\-]?=)[^;\n]*;|\w+\([^;\n]*\);)[ \t]*$", src):
        i, j = m.start(1), m.end(1)
        if mask[i] and not src[i:j].startswith("return"):
            add(i, j, ";", "SDL")
    return out


def main():
    d = sys.argv[1]
    outp = sys.argv[2]
    suites = {}
    for arg in sys.argv[3:]:
        name, fn = arg.split("=", 1)
        suites[name] = parse_vec(fn)
    ref_src = open(os.path.join(LAB, d, "ref", "v1.0", IMPL[d])).read()
    pub = os.path.join(LAB, d, "public", "v1.0")
    all_blocks = []
    owner = []
    for name, blocks in suites.items():
        for b in blocks:
            all_blocks.append(b)
            owner.append(name)
    tmp = tempfile.mkdtemp()

    def run_src(src, tag):
        bd = os.path.join(tmp, tag)
        os.makedirs(bd, exist_ok=True)
        shutil.copy(os.path.join(pub, "driver.c"), bd)
        shutil.copy(os.path.join(pub, HDR[d]), bd)
        open(os.path.join(bd, IMPL[d]), "w").write(src)
        exe = os.path.join(bd, "drv")
        r = subprocess.run(["cc", "-std=c11", "-O1", "-w", "-o", exe, os.path.join(bd, "driver.c"),
                            os.path.join(bd, IMPL[d]), "-lm"], capture_output=True)
        if r.returncode != 0:
            return None
        res = run_blocks(exe, all_blocks, timeout=20)
        shutil.rmtree(bd, ignore_errors=True)
        return res

    base = run_src(ref_src, "base")
    valid = [r["ok"] for r in base]
    for name in suites:
        tot = sum(1 for o in owner if o == name)
        ok = sum(1 for o, v in zip(owner, valid) if o == name and v)
        print(f"suite {name}: {ok}/{tot} blocks agree with the reference")
    muts = mutants(ref_src)
    print(f"{len(muts)} mutants")

    def one(k):
        m = muts[k]
        res = run_src(m["src"], f"m{k}")
        if res is None:
            return {"k": k, "compiled": False}
        killed = {}
        for name in suites:
            killed[name] = any((not r["ok"]) for r, o, v in zip(res, owner, valid) if o == name and v)
        return {"k": k, "compiled": True, "killed": killed,
                "failed_ids": {name: [r["id"] for r, o, v in zip(res, owner, valid) if o == name and v and not r["ok"]]
                               for name in suites}}

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(one, range(len(muts))))
    shutil.rmtree(tmp, ignore_errors=True)
    rows = []
    for m, r in zip(muts, results):
        rows.append({k: m[k] for k in ("op", "line", "orig", "new")} | {kk: r[kk] for kk in r if kk != "k"})
    comp = [r for r in rows if r["compiled"]]
    summary = {"mutants": len(rows), "compiled": len(comp)}
    oracle_name = "oracle"
    killable = [r for r in comp if r["killed"].get(oracle_name)]
    summary["killable_by_oracle"] = len(killable)
    for name in suites:
        summary[f"kill_{name}"] = sum(r["killed"][name] for r in killable)
    summary["suite_validity"] = {name: [sum(1 for o, v in zip(owner, valid) if o == name and v),
                                        sum(1 for o in owner if o == name)] for name in suites}
    json.dump({"summary": summary, "rows": rows}, open(outp, "w"))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()

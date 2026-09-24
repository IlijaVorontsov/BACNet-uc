#!/usr/bin/env python3
"""Stage 3 (evolution) and Stage 4 (review) scoring.

  analyze_s34.py s3 WSROOT OUT.json [MAXSTEP]
  analyze_s34.py s4 WSROOT OUT.json
"""
import glob
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from run_vectors import parse_vec, run_blocks  # noqa: E402
from lab import build, ORACLE  # noqa: E402

IMPL = {"p": "bacapp.c", "x": "alarm.c"}
STEPVER = {1: "1.1", 2: "1.2", 3: "1.3"}
CONFLICT = {"p": {"CR3nan": "NaN policy (private decision)", "CR3zero": "Unsigned 0 encoding (BACnet standard)"},
            "x": {"CR3clear": "ack survives return-to-normal (private safety policy)",
                  "CR3incl": "strict limits (BACnet 13.3.6)"}}
GEN = {"p": ["CR1gen"], "x": []}


def oracle_blocks(d, ver):
    return parse_vec(os.path.join(LAB, d, "oracle", ORACLE[(d, ver)] + ".vec"))


def classify(d, ver):
    """block id -> class: old / new / conflict:<tag> / gen:<tag>"""
    base = {b["id"]: b["exp"] for b in oracle_blocks(d, "1.0")}
    cls = {}
    for b in oracle_blocks(d, ver):
        conf = [t for t in b["tags"] if t in CONFLICT[d]]
        gen = [t for t in b["tags"] if t in GEN[d]]
        if conf and ver == "1.3":
            cls[b["id"]] = "conflict:" + conf[0]
        elif ver == "1.3" and "CR3utf8" in b["tags"]:
            cls[b["id"]] = "item1:CR3utf8"
        elif gen:
            cls[b["id"]] = "gen:" + gen[0]
        elif b["id"] in base and base[b["id"]] == b["exp"]:
            cls[b["id"]] = "old"
        else:
            cls[b["id"]] = "new"
    return cls


def rate(res, ids):
    ids = set(ids)
    sel = [r for r in res if r["id"] in ids]
    return [sum(r["ok"] for r in sel), len(sel)]


def s3(root, outp, maxstep):
    tmp = tempfile.mkdtemp()
    out = {}
    vis = {d: {b["id"] for b in parse_vec(os.path.join(LAB, d, "oracle", "visible_unit.vec"))} for d in ("p", "x")}
    for d in ("p", "x"):
        for step in range(1, maxstep + 1):
            ver = STEPVER[step]
            blocks = oracle_blocks(d, ver)
            cls = classify(d, ver)
            acc = parse_vec(os.path.join(LAB, d, "acceptance_v1.3.vec" if (d == "x" and ver == "1.3") else "acceptance.vec"))
            for ws in sorted(glob.glob(os.path.join(root, "s3", d, "*", "*", f"cr{step}"))):
                arm, rep = ws.split(os.sep)[-3:-1]
                key = f"{d}/{arm}/{rep}/cr{step}"
                exe, how = build(d, ver, ws, tmp)
                if exe is None:
                    res = [{"id": b["id"], "ok": False, "status": how} for b in blocks]
                    ares = []
                else:
                    res = run_blocks(exe, blocks)
                    ares = run_blocks(exe, acc)
                old = [i for i, c in cls.items() if c == "old"]
                r = {"domain": d, "arm": arm, "rep": int(rep), "step": step, "build": how,
                     "acceptance": [sum(x["ok"] for x in ares), len(acc)],
                     "old": rate(res, old),
                     "old_visible": rate(res, [i for i in old if i in vis[d]]),
                     "old_heldout": rate(res, [i for i in old if i not in vis[d]]),
                     "new": rate(res, [i for i, c in cls.items() if c == "new"]),
                     "all": rate(res, list(cls)),
                     "fails_old": [x["id"] for x in res if cls.get(x["id"]) == "old" and not x["ok"]]}
                for tag in list(CONFLICT[d]) + GEN[d] + (["CR3utf8"] if d == "p" else []):
                    ids = [i for i, c in cls.items() if c.endswith(":" + tag)]
                    if ids:
                        r[tag] = rate(res, ids)
                notes = os.path.join(ws, "NOTES.md")
                r["notes"] = open(notes).read()[-6000:] if os.path.exists(notes) else ""
                out[key] = r
                print(key, how, "old", r["old"], "new", r["new"], "acc", r["acceptance"],
                      {t: r[t] for t in list(CONFLICT[d]) + GEN[d] if t in r})
    shutil.rmtree(tmp, ignore_errors=True)
    json.dump(out, open(outp, "w"), indent=1)


def s4(root, outp):
    tmp = tempfile.mkdtemp()
    fp = json.load(open(os.path.join(LAB, "bug_fingerprints.json")))
    out = {}
    for d in ("p", "x"):
        blocks = oracle_blocks(d, "1.0")
        mod = __import__("gen_p" if d == "p" else "gen_x")
        cats = mod.RULES
        bug_ws = tempfile.mkdtemp(dir=tmp)
        shutil.copy(os.path.join(LAB, d, "bugs", IMPL[d]), bug_ws)
        bexe, _ = build(d, "1.0", bug_ws, tmp)
        base = {r["id"]: r["ok"] for r in run_blocks(bexe, blocks)}
        tagmap = {b["id"]: b["tags"] for b in blocks}
        for ws in sorted(glob.glob(os.path.join(root, "s4", d, "*", "*"))):
            arm, rep = ws.split(os.sep)[-2:]
            exe, how = build(d, "1.0", ws, tmp)
            res = run_blocks(exe, blocks) if exe else [{"id": b["id"], "ok": False} for b in blocks]
            now = {r["id"]: r["ok"] for r in res}
            # unique fingerprint: blocks broken by this bug alone (not by any other injected bug)
            uniq = {bug: [i for i in ids if not any(i in o for ob, o in fp[d].items() if ob != bug)] or ids
                    for bug, ids in fp[d].items()}
            fixed = {bug: all(now[i] for i in ids) for bug, ids in uniq.items()}
            partial = {bug: sum(now[i] for i in ids) / len(ids) for bug, ids in uniq.items()}
            regress = [i for i in now if base[i] and not now[i]]
            reg_rules = {}
            for i in regress:
                rules = [t for t in tagmap[i] if t in cats]
                prim = rules[0] if rules else "?"
                reg_rules[prim] = reg_rules.get(prim, 0) + 1
            review = os.path.join(ws, "REVIEW.md")
            r = {"domain": d, "arm": arm, "rep": int(rep), "build": how, "fixed": fixed, "partial": partial,
                 "n_regress": len(regress), "regress_by_rule": reg_rules,
                 "regress_by_cat": {c: sum(v for k, v in reg_rules.items() if cats.get(k) == c) for c in ("STD", "POL", "MIX")},
                 "passed": sum(now.values()), "total": len(now),
                 "review": open(review).read()[-8000:] if os.path.exists(review) else ""}
            out[f"{d}/{arm}/{rep}"] = r
            print(f"{d}/{arm}/{rep}", how, "fixed", sum(fixed.values()), "/", len(fixed), "regress", len(regress), r["regress_by_cat"])
    shutil.rmtree(tmp, ignore_errors=True)
    json.dump(out, open(outp, "w"), indent=1)


if __name__ == "__main__":
    if sys.argv[1] == "s3":
        s3(sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 3)
    else:
        s4(sys.argv[2], sys.argv[3])

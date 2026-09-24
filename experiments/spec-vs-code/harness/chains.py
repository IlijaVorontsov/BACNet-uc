#!/usr/bin/env python3
"""Stage 3 (evolution) and Stage 4 (review) workspace management.

  chains.py s3init  WSROOT          create cr1 workspaces for every (domain, arm, rep)
  chains.py s3next  WSROOT STEP     create cr<STEP> from cr<STEP-1> for every chain
  chains.py s4init  WSROOT          create review workspaces

Arms:  A  = code + acceptance test                         ("the code is everything")
       Ac = code with the spec embedded as comments + acceptance test
       B  = code + SPEC.md + acceptance test
       C  = code + unit tests + acceptance test
       D  = code + SPEC.md + unit tests + acceptance test
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
IMPL = {"p": "bacapp.c", "x": "alarm.c"}
HDR = {"p": "bacapp.h", "x": "alarm.h"}
ARMS = ["A", "Ac", "B", "C", "D"]
REPS = [1, 2, 3]
# public version used for each CR step
STEPVER = {1: "1.1", 2: "1.2", 3: "1.3"}


def pub(d, ver):
    return os.path.join(LAB, d, "public", "v" + ver)


def acceptance(d, ver):
    if d == "x" and ver == "1.3":
        return os.path.join(LAB, d, "acceptance_v1.3.vec")
    return os.path.join(LAB, d, "acceptance.vec")


def base_impl(d, arm):
    if arm == "Ac":
        return os.path.join(LAB, d, "ref", "v1.0-commented", IMPL[d])
    return os.path.join(LAB, d, "ref", "v1.0", IMPL[d])


def merge_acceptance(new_src, dst):
    """Replace the user's original acceptance blocks by their new version, keep blocks the agent added."""
    sys.path.insert(0, HERE)
    from run_vectors import parse_vec
    from lab import write_vec
    new = {b["id"]: b for b in parse_vec(new_src)}
    cur = parse_vec(dst) if os.path.exists(dst) else []
    out = [new.get(b["id"], b) for b in cur]
    have = {b["id"] for b in out}
    out += [b for i, b in new.items() if i not in have]
    write_vec(out, dst)


def s3init(root, doms=("p", "x")):
    for d in doms:
        for arm in ARMS:
            for rep in REPS:
                ws = os.path.join(root, "s3", d, arm, str(rep), "cr1")
                if os.path.exists(ws):
                    shutil.rmtree(ws)
                os.makedirs(ws)
                for fn in os.listdir(pub(d, "1.1")):
                    shutil.copy(os.path.join(pub(d, "1.1"), fn), ws)
                shutil.copy(os.path.join(HERE, "run_vectors.py"), ws)
                shutil.copy(acceptance(d, "1.1"), os.path.join(ws, "acceptance.vec"))
                shutil.copy(base_impl(d, arm), os.path.join(ws, IMPL[d]))
                if arm in ("B", "D"):
                    shutil.copy(os.path.join(LAB, d, "SPEC.md"), ws)
                if arm in ("C", "D"):
                    os.makedirs(os.path.join(ws, "tests"))
                    shutil.copy(os.path.join(LAB, d, "oracle", "visible_unit.vec"), os.path.join(ws, "tests", "unit.vec"))
                shutil.copy(os.path.join(LAB, d, "crs", "CR1.md"), os.path.join(ws, "CR.md"))
    print("s3 cr1 workspaces created")


def s3next(root, step, doms=("p", "x"), arms=None):
    prev_ver, ver = STEPVER[step - 1], STEPVER[step]
    for d in doms:
        for arm in (arms or ARMS):
            for rep in REPS:
                src = os.path.join(root, "s3", d, arm, str(rep), f"cr{step-1}")
                dst = os.path.join(root, "s3", d, arm, str(rep), f"cr{step}")
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("drv", "*.o"))
                for fn in ("driver.c", "Makefile"):
                    shutil.copy(os.path.join(pub(d, ver), fn), dst)
                shutil.copy(os.path.join(HERE, "run_vectors.py"), dst)
                old_h = open(os.path.join(pub(d, prev_ver), HDR[d])).read()
                new_h = open(os.path.join(pub(d, ver), HDR[d])).read()
                if old_h != new_h:
                    shutil.copy(os.path.join(pub(d, ver), HDR[d]), dst)
                if acceptance(d, ver) != acceptance(d, prev_ver):
                    merge_acceptance(acceptance(d, ver), os.path.join(dst, "acceptance.vec"))
                shutil.copy(os.path.join(LAB, d, "crs", f"CR{step}.md"), os.path.join(dst, "CR.md"))
    print(f"s3 cr{step} workspaces created")


S4ARMS = ["A", "Ac", "B", "C"]


def s4init(root, doms=("p", "x")):
    for d in doms:
        for arm in S4ARMS:
            for rep in REPS:
                ws = os.path.join(root, "s4", d, arm, str(rep))
                if os.path.exists(ws):
                    shutil.rmtree(ws)
                os.makedirs(ws)
                for fn in os.listdir(pub(d, "1.0")):
                    shutil.copy(os.path.join(pub(d, "1.0"), fn), ws)
                shutil.copy(os.path.join(HERE, "run_vectors.py"), ws)
                shutil.copy(acceptance(d, "1.0"), os.path.join(ws, "acceptance.vec"))
                src = os.path.join(LAB, d, "bugs", "commented" if arm == "Ac" else "", IMPL[d])
                shutil.copy(src, os.path.join(ws, IMPL[d]))
                if arm == "B":
                    shutil.copy(os.path.join(LAB, d, "SPEC.md"), ws)
                if arm == "C":
                    os.makedirs(os.path.join(ws, "tests"))
                    shutil.copy(os.path.join(LAB, d, "oracle", "visible_unit_s4.vec"), os.path.join(ws, "tests", "unit.vec"))
    print("s4 workspaces created")


if __name__ == "__main__":
    cmd = sys.argv[1]
    doms = tuple(sys.argv[-1].split(",")) if sys.argv[-1] in ("p", "x", "p,x") else ("p", "x")
    if cmd == "s3init":
        s3init(sys.argv[2], doms)
    elif cmd == "s3next":
        s3next(sys.argv[2], int(sys.argv[3]), doms)
    elif cmd == "s3adv":  # s3adv WSROOT STEP DOMAIN ARM
        s3next(sys.argv[2], int(sys.argv[3]), (sys.argv[4],), [sys.argv[5]])
    elif cmd == "s4init":
        s4init(sys.argv[2], doms)

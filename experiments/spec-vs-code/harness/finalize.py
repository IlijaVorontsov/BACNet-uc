#!/usr/bin/env python3
"""Copy the experiment (materials, harness, agent workspaces, results) into the repository.

usage: finalize.py SCRATCH REPO_DEST
"""
import os
import shutil
import sys

SCR, DEST = sys.argv[1], sys.argv[2]
LAB = os.path.join(SCR, "lab")
TEXT_EXT = {".c", ".h", ".md", ".vec", ".py", ".inp", ".json", ".txt"}
MAX = 1_000_000  # skip generated files above 1 MB


def keep(fn, path):
    base = os.path.basename(fn)
    if base == "Makefile":
        return True
    if "__pycache__" in path:
        return False
    ext = os.path.splitext(base)[1]
    if ext not in TEXT_EXT:
        return False
    return os.path.getsize(path) <= MAX


def copy_tree(src, dst, skip_names=()):
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in ("__pycache__",) and d not in skip_names]
        for f in files:
            p = os.path.join(root, f)
            if not keep(f, p):
                continue
            rel = os.path.relpath(p, src)
            q = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(q), exist_ok=True)
            shutil.copy2(p, q)
            n += 1
    return n


if os.path.exists(DEST):
    shutil.rmtree(DEST)
os.makedirs(DEST)
names = {"p": "bacapp", "x": "alarm"}
for d, name in names.items():
    n = copy_tree(os.path.join(LAB, d), os.path.join(DEST, "domains", f"{d}-{name}"))
    print(f"domains/{d}-{name}: {n} files")
print("harness:", copy_tree(os.path.join(LAB, "harness"), os.path.join(DEST, "harness")))
print("results:", copy_tree(os.path.join(LAB, "results"), os.path.join(DEST, "results")))
print("runs:", copy_tree(os.path.join(SCR, "ws"), os.path.join(DEST, "runs")))
print("runs_clean:", copy_tree(os.path.join(SCR, "ws_clean"), os.path.join(DEST, "runs-clean-tests")))
shutil.copy2(os.path.join(LAB, "bug_fingerprints.json"), os.path.join(DEST, "results"))

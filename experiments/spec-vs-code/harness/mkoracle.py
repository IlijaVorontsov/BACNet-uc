#!/usr/bin/env python3
"""mkoracle.py INP DRV OUT.vec : fill expected outputs of INP blocks from reference driver DRV."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_vectors import parse_vec, run_blocks
inp, drv, out = sys.argv[1:4]
blocks = parse_vec(inp)
res = run_blocks(os.path.abspath(drv), blocks)
bad = [r for r in res if r["status"] != "ok" or len(r["got"]) != len(blocks[res.index(r)]["inp"])]
if bad:
    raise SystemExit(f"reference problem in {len(bad)} blocks, first: {bad[0]}")
with open(out, "w") as f:
    for b, r in zip(blocks, res):
        f.write(f"### {b['id']} {' '.join(b['tags'])}\n")
        for ln in b["inp"]:
            f.write(ln + "\n")
        f.write("---\n")
        for ln in r["got"]:
            f.write(ln + "\n")
print(f"{out}: {len(blocks)} blocks")

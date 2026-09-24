#!/usr/bin/env python3
"""Stage 1 probes: for concrete intent questions, how did each one-shot implementation decide?"""
import glob, json, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); LAB = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from run_vectors import parse_vec, run_blocks
from lab import build
WS = sys.argv[1]; OUT = sys.argv[2]
P_ORACLE = {b["id"]: b for b in parse_vec(f"{LAB}/p/oracle/v1.0.vec")}
def pid(cmd):
    for i, b in P_ORACLE.items():
        if b["inp"] == [cmd]: return i
    raise KeyError(cmd)
PROBES = {
 "p": [
  ("NaN REAL is refused (private decision D-7)", "E5a", pid("enc_real 7fc00000")),
  ("bit value 2 is rejected (private)", "E8a", pid("enc_bits 2")),
  ("decoder accepts non-minimal unsigned 22 00 05 (private leniency)", "D4", pid("dec_app 220005")),
  ("decoder accepts extended length for a short value 65 03 .. (private leniency)", "D1b", pid("dec_app 650300aabb")),
  ("decoder does NOT range-check date fields (private)", "D9", pid("dec_app a400000000")),
  ("encoder does NOT validate UTF-8 (private)", "E7a", pid("enc_str ff")),
  ("-128 encodes as 31 80 (standard)", "E4", pid("enc_signed -128")),
  ("length 253 uses 1 length octet (standard)", "G4", pid("enc_octets rep:ab:253")),
  ("context boolean has a content octet (standard)", "E13", pid("enc_ctx_bool 3 1")),
  ("no partial write when cap too small (private G1)", "G1", pid("enc_octets rep:11:5 @6")),
 ],
 "x": [
  ("value exactly at the limit is NOT an alarm (BACnet)", "A2", "x001"),
  ("direct HIGH->LOW without passing NORMAL (BACnet)", "A4", "x011"),
  ("+/-inf is a value, not a sensor fault (private)", "A6", "x028"),
  ("returned-to-normal alarm still escalates if unacked (private SP-3)", "A8", "x037"),
  ("escalate_after = 0 disables escalation (private)", "A9", "x042"),
  ("ack in the same call that fires a delayed alarm acknowledges it (private)", "A5a", "x021"),
  ("maintenance exit sends catch-up for silent alarm (private)", "A10", "x049"),
  ("catch-up alarm after maintenance starts an escalation episode (private)", "A10", "x050"),
  ("fault recovery with out-of-range value & delay 0 gives TO_NORMAL+TO_HIGH (private)", "A6a", "x030"),
  ("calls with time going backwards are ignored (private)", "A12", "x059"),
  ("expired-but-unobserved delay is cancelled by a normal sample (private)", "A5a", "x019"),
 ],
}
res = {}
tmp = tempfile.mkdtemp()
for d in ("p", "x"):
    oracle = {b["id"]: b for b in parse_vec(f"{LAB}/{d}/oracle/v1.0.vec")}
    impls = sorted(glob.glob(f"{WS}/s1/{d}/*/*"))
    res[d] = []
    for q, rule, bid in PROBES[d]:
        row = {"question": q, "rule": rule, "id": bid, "expected": oracle[bid]["exp"], "answers": {}}
        for ws in impls:
            arm, rep = ws.split("/")[-2:]
            exe, _ = build(d, "1.0", ws, tmp)
            r = run_blocks(exe, [oracle[bid]])[0]
            row["answers"][f"{arm}-{rep}"] = {"ok": r["ok"], "got": r["got"]}
        res[d].append(row)
        a = row["answers"]
        po = [k for k in a if not k.startswith("iii")]
        print(f"{d} {rule:4} prompt-only {sum(a[k]['ok'] for k in po)}/{len(po)}  spec {sum(a[k]['ok'] for k in a if k.startswith('iii'))}/4  | {q}")
json.dump(res, open(OUT, "w"), indent=1)

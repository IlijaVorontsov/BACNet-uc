#!/usr/bin/env python3
"""Aggregate all result files into markdown tables (results/tables.md) and a summary JSON.

usage: report_tables.py RESULTS_DIR
expects: s1.json mutation_p.json mutation_x.json s3.json s4.json costs.json
"""
import json
import os
import statistics as st
import sys

R = sys.argv[1]
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_p  # noqa: E402
import gen_x  # noqa: E402

DOM = {"p": "P · BACnet codec", "x": "X · alarm policy"}
CATS = {"p": gen_p.RULES, "x": gen_x.RULES}
out = []
summary = {}


def load(name):
    p = os.path.join(R, name)
    return json.load(open(p)) if os.path.exists(p) else None


def pct(a, b):
    return 100.0 * a / b if b else float("nan")


def fmt(x, nd=0):
    return "–" if x is None or x != x else f"{x:.{nd}f}"


def mm(vals, nd=0):
    vals = [v for v in vals if v == v]
    if not vals:
        return "–"
    if len(vals) == 1:
        return fmt(vals[0], nd)
    return f"{fmt(st.mean(vals), nd)} ({fmt(min(vals), nd)}–{fmt(max(vals), nd)})"


def rule_rate(run, cat, d):
    """pass rate over targeted (non-random) vectors whose primary rule has category cat"""
    ok = tot = 0
    for rule, (a, b) in run["by_rule"].items():
        pass
    return None


# ---------------- Stage 1 ----------------
s1 = load("s1.json")
if s1:
    out.append("## Stage 1 — one-shot creation\n")
    out.append("Oracle = hidden intent (all spec rules). STD = rules fixed by the public BACnet standard; POL = private "
               "product decisions; MIX = random/fuzz vectors that exercise many rules at once. Values: mean (min–max) over 4 runs.\n")
    out.append("| Domain | Artifacts given to the agent | Acceptance test passed | Oracle match % | STD % | POL % | MIX % |")
    out.append("|---|---|---|---|---|---|---|")
    names = {"i": "prompt + acceptance test", "ii": "prompt + acceptance test, asked to write unit tests",
             "iii": "prompt + acceptance test + SPEC.md"}
    summary["s1"] = {}
    for d in ("p", "x"):
        runs = s1[d]["runs"]
        for arm in ("i", "ii", "iii"):
            rs = [r for k, r in runs.items() if r.get("arm") == arm and "passed" in r]
            acc = sum(1 for r in rs if r["acceptance"][0] == r["acceptance"][1])
            orc = [pct(r["passed"], r["total"]) for r in rs]
            cat = {c: [pct(*r["by_cat"][c]) for r in rs if c in r["by_cat"]] for c in ("STD", "POL", "MIX")}
            out.append(f"| {DOM[d]} | {names[arm]} | {acc}/{len(rs)} | {mm(orc,1)} | {mm(cat['STD'],1)} | {mm(cat['POL'],1)} | {mm(cat['MIX'],1)} |")
            summary["s1"][f"{d}-{arm}"] = {"acceptance": [acc, len(rs)], "oracle": orc, **{c: v for c, v in cat.items()}}
    out.append("")
    # divergence
    out.append("**Divergence between independent one-shot implementations** (share of oracle inputs on which two "
               "implementations produce different output):\n")
    out.append("| Domain | Pairs within prompt-only arms (i+ii) | Pairs within spec arm (iii) |")
    out.append("|---|---|---|")
    for d in ("p", "x"):
        pd = s1[d]["pairwise_diff"]
        a = [v * 100 for k, v in pd.items() if all(x.split("-")[0] in ("i", "ii") for x in k.split("|"))]
        b = [v * 100 for k, v in pd.items() if all(x.split("-")[0] == "iii" for x in k.split("|"))]
        out.append(f"| {DOM[d]} | {mm(a,1)} over {len(a)} pairs | {mm(b,1)} over {len(b)} pairs |")
        summary["s1"][f"{d}-divergence"] = {"prompt_only": a, "spec": b}
    out.append("")
    # distinct behaviours per block
    for d in ("p", "x"):
        dist = s1[d]["distinct_per_block"]
        n = len(dist)
        multi = sum(1 for v in dist.values() if v > 1)
        summary["s1"][f"{d}-blocks_with_disagreement"] = [multi, n]
    # self tests
    out.append("**Unit tests the agent wrote for its own code (arm ii)** — checked against the hidden intent:\n")
    out.append("| Domain | Run | Test blocks | Pass on own code | Agree with the intended behaviour |")
    out.append("|---|---|---|---|---|")
    for d in ("p", "x"):
        for k, r in sorted(s1[d]["runs"].items()):
            stt = r.get("self_tests")
            if stt:
                lab = k + (" (spec arm, wrote tests unprompted)" if k.startswith("iii") else "")
                out.append(f"| {DOM[d]} | {lab} | {stt['blocks']} | {stt['pass_own']} | {stt['agree_with_intent']} ({fmt(pct(stt['agree_with_intent'], stt['blocks']),0)} %) |")
    out.append("")

# ---------------- Stage 2 ----------------
for d in ("p", "x"):
    m = load(f"mutation_{d}.json")
    if not m:
        continue
    s = m["summary"]
    if d == "p" or "## Stage 2" not in "\n".join(out):
        out.append("## Stage 2 — how many injected faults does each kind of test catch? (mutation analysis)\n")
        out.append("Mutants: single-point faults injected into the correct reference implementation. Denominator = "
                   "mutants the full oracle detects (the rest are behaviour-preserving). Only test blocks that agree "
                   "with the intended behaviour are used (wrong expectations would 'detect' the correct code).\n")
        out.append("| Domain | Mutants detectable | Acceptance test | Visible unit tests (spec-derived, 50 %) | Agent self-written unit tests (4 suites) | Black-box tests written from SPEC.md (2 suites) |")
        out.append("|---|---|---|---|---|---|")
    K = s["killable_by_oracle"]
    selfs = [pct(s[f"kill_self{i}"], K) for i in (1, 2, 3, 4) if f"kill_self{i}" in s]
    bbs = [pct(s[f"kill_specbb{i}"], K) for i in (1, 2) if f"kill_specbb{i}" in s]
    out.append(f"| {DOM[d]} | {K} of {s['compiled']} | {fmt(pct(s['kill_acceptance'], K),1)} % | "
               f"{fmt(pct(s['kill_unit_visible'], K),1)} % | {mm(selfs,1)} % | {mm(bbs,1)} % |")
    summary[f"mutation_{d}"] = s
out.append("")

# ---------------- Stage 3 ----------------
s3 = load("s3.json")
s3c = load("s3_clean.json")
if s3 and s3c:
    s3_contam = {k: v for k, v in s3.items() if v["arm"] in ("C", "D")}
    s3 = {k: v for k, v in s3.items() if v["arm"] not in ("C", "D")}
    s3.update({"clean/" + k: v for k, v in s3c.items()})
    for k, v in s3_contam.items():
        v = dict(v); v["arm"] = v["arm"] + "*"; s3["contam/" + k] = v
if s3:
    out.append("## Stage 3 — evolution: three change requests applied by fresh agents (no memory)\n")
    out.append("Base = the correct reference code. Each chain: CR1 feature → CR2 refactor → CR3 field requests "
               "(one legitimate item + items that conflict with earlier decisions). 3 chains per arm. "
               "'Kept' = previously intended behaviour still intact (hidden oracle). Values are means over 3 chains.\n")
    arms = {"A": "code + acceptance test", "Ac": "code with spec as comments + acceptance",
            "B": "code + SPEC.md + acceptance", "C": "code + unit tests + acceptance",
            "D": "code + SPEC.md + unit tests + acceptance",
            "C*": "(C with leaked labels — not used for conclusions)", "D*": "(D with leaked labels — not used for conclusions)"}
    summary["s3"] = {}
    for d in ("p", "x"):
        out.append(f"### {DOM[d]}\n")
        conf = {"p": [("CR3nan", "NaN refusal kept (private decision)"), ("CR3zero", "Unsigned 0 = 21 00 kept (standard)")],
                "x": [("CR3clear", "ack survives return-to-normal kept"), ("CR3incl", "strict limits kept (standard)")]}[d]
        hdr = "| Arm | Old behaviour kept after CR1 / CR2 / CR3 (%) | Held-out old behaviour after CR3 (%) | New features correct CR1 / CR3 (%) | " + \
              " | ".join(c[1] for c in conf) + (" | NaN policy carried to new Double (CR1) | CR3 item 1 (UTF-8 check; conflicts with POL rule E7a) |" if d == "p" else " |")
        out.append(hdr)
        out.append("|" + "---|" * (hdr.count("|") - 1))
        for arm in ("A", "Ac", "B", "C", "D", "C*", "D*"):
            row = {}
            for step in (1, 2, 3):
                rs = [v for k, v in s3.items() if v["domain"] == d and v["arm"] == arm and v["step"] == step]
                row[step] = rs
            def m_(rs, key):
                vals = [pct(*r[key]) for r in rs if key in r and r[key][1]]
                return st.mean(vals) if vals else float("nan")
            old = " / ".join(fmt(m_(row[s], "old"), 1) for s in (1, 2, 3))
            held = fmt(m_(row[3], "old_heldout"), 1)
            new = f"{fmt(m_(row[1], 'new'),1)} / {fmt(m_(row[3], 'new'),1)}"
            confs = " | ".join(f"{sum(1 for r in row[3] if c[0] in r and r[c[0]][0] == r[c[0]][1])}/{len(row[3])}" for c in conf)
            line = f"| {arms[arm]} | {old} | {held} | {new} | {confs} |"
            if d == "p":
                g = [r for r in row[1] if "CR1gen" in r]
                u = [r for r in row[3] if "CR3utf8" in r]
                line += f" {sum(1 for r in g if r['CR1gen'][0] == r['CR1gen'][1])}/{len(g)} | {sum(1 for r in u if r['CR3utf8'][0] == r['CR3utf8'][1])}/{len(u)} implemented |"
            out.append(line)
            summary["s3"][f"{d}-{arm}"] = {str(s): [{k: r.get(k) for k in ("old", "old_heldout", "new", "all", "CR3nan", "CR3zero", "CR3clear", "CR3incl", "CR1gen", "CR3utf8", "acceptance")} for r in row[s]] for s in (1, 2, 3)}
        out.append("")

# ---------------- Stage 4 ----------------
s4 = load("s4.json")
s4c = load("s4_clean.json")
if s4 and s4c:
    contam = {k: dict(v, arm="C*") for k, v in s4.items() if v["arm"] == "C"}
    s4 = {k: v for k, v in s4.items() if v["arm"] != "C"}
    s4.update({"clean/" + k: v for k, v in s4c.items()})
    s4.update({"contam/" + k: v for k, v in contam.items()})
if s4:
    out.append("## Stage 4 — review: 'the module has some bugs, fix them' (5 injected bugs, deliberate quirks present)\n")
    out.append("| Domain | Artifacts | Bugs that violate the standard, fixed | Bugs that violate only private policy, fixed | Intended behaviour broken (oracle vectors per run) |")
    out.append("|---|---|---|---|---|")
    names = {"A": "code + acceptance", "Ac": "code with spec as comments", "B": "code + SPEC.md", "C": "code + unit tests",
             "C*": "(code + unit tests with leaked labels — not used)"}
    summary["s4"] = {}
    for d in ("p", "x"):
        for arm in ("A", "Ac", "B", "C", "C*"):
            rs = [v for k, v in s4.items() if v["domain"] == d and v["arm"] == arm]
            std = sum(ok for r in rs for b, ok in r["fixed"].items() if "[STD]" in b)
            stdn = sum(1 for r in rs for b in r["fixed"] if "[STD]" in b)
            pol = sum(ok for r in rs for b, ok in r["fixed"].items() if "[POL]" in b)
            poln = sum(1 for r in rs for b in r["fixed"] if "[POL]" in b)
            reg = [r["n_regress"] for r in rs]
            out.append(f"| {DOM[d]} | {names[arm]} | {std}/{stdn} | {pol}/{poln} | {mm(reg,1)} |")
            summary["s4"][f"{d}-{arm}"] = {"std": [std, stdn], "pol": [pol, poln], "regress": reg,
                                           "per_bug": {b: sum(r['fixed'][b] for r in rs) for b in rs[0]["fixed"]} if rs else {}}
    out.append("")

# ---------------- cost ----------------
c = load("costs.json")
if c:
    out.append("## Cost per agent run (from transcripts)\n")
    out.append("| Stage / arm | Runs | Wall time per run, min (mean) | Tool calls per run (mean) |")
    out.append("|---|---|---|---|")
    groups = {}
    for lab, v in c.items():
        clean = lab.startswith("c-")
        parts = (lab[2:] if clean else lab).split("-")
        if clean:
            key = f"{parts[0]} {parts[1]} arm {parts[2]} (clean tests)"
        elif parts[0] == "s3":
            key = f"s3 {parts[1]} arm {parts[2]}"
        elif parts[0] == "s4":
            key = f"s4 {parts[1]} arm {parts[2]}"
        elif parts[0] == "s1":
            key = f"s1 {parts[1]} arm {parts[2]}"
        else:
            key = f"s0 {parts[1]} {parts[2].rstrip('0123456789')}"
        groups.setdefault(key, []).append(v)
    for key in sorted(groups):
        vs = groups[key]
        out.append(f"| {key} | {len(vs)} | "
                   f"{fmt(st.mean((v['wall_s'] or 0) / 60 for v in vs),1)} | {fmt(st.mean(v['tool_calls'] for v in vs),1)} |")
    viol = {k: v["hidden_dir_accesses"] for k, v in c.items() if v["hidden_dir_accesses"]}
    out.append(f"\nBlinding audit: {len(viol)} of {len(c)} agent transcripts contain a tool call touching the hidden lab directory.\n")
    summary["blinding_violations"] = viol

open(os.path.join(R, "tables.md"), "w").write("\n".join(out) + "\n")
json.dump(summary, open(os.path.join(R, "summary.json"), "w"), indent=1)
print("\n".join(out))

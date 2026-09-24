#!/usr/bin/env python3
"""Extract per-agent cost (output tokens, input+cache tokens, wall time, tool calls) from workflow transcripts,
keyed by agent label. Also audits blinding: flags any tool call that touches the hidden lab directory.
usage: costs.py OUT.json"""
import glob, json, os, sys
from datetime import datetime
D = "/root/.claude/projects/-home-user-BACNet-uc/2902ee81-7979-532f-bc88-6a21c18ba069/subagents/workflows"
HIDDEN = "/scratchpad/lab"
out = {}
for wf in sorted(glob.glob(D + "/wf_*")):
    labels = {}
    for line in open(wf + "/journal.jsonl"):
        e = json.loads(line)
        if e.get("type") == "started":
            labels[e["agentId"]] = e["label"]
    for aid, label in labels.items():
        fn = f"{wf}/agent-{aid}.jsonl"
        if not os.path.exists(fn):
            continue
        otok = itok = calls = 0
        ts = []
        leaks = []
        for line in open(fn):
            try:
                m = json.loads(line)
            except Exception:
                continue
            if "timestamp" in m:
                ts.append(m["timestamp"])
            msg = m.get("message") or {}
            if m.get("type") == "assistant" and isinstance(msg, dict):
                u = msg.get("usage") or {}
                otok += u.get("output_tokens", 0)
                itok += u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                for c in msg.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        calls += 1
                        s = json.dumps(c.get("input"))
                        if HIDDEN in s:
                            leaks.append(s[:300])
        wall = None
        if len(ts) > 1:
            f = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))
            wall = (f(max(ts)) - f(min(ts))).total_seconds()
        out[label] = {"wf": os.path.basename(wf), "agent": aid, "output_tokens": otok, "input_tokens": itok,
                      "tool_calls": calls, "wall_s": wall, "hidden_dir_accesses": leaks}
json.dump(out, open(sys.argv[1], "w"), indent=1)
bad = {k: v["hidden_dir_accesses"] for k, v in out.items() if v["hidden_dir_accesses"]}
print(f"{len(out)} agents; blinding violations: {len(bad)}")
for k, v in bad.items():
    print(" ", k, v[:2])

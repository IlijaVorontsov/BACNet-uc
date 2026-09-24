#!/usr/bin/env python3
"""Review aid: independent model of SPEC.md, fuzzed against ./drv. Values are chosen
exactly representable so float32 vs double arithmetic cannot differ."""
import math, random, subprocess, sys

NORMAL, HIGH, LOW, FAULT = "NORMAL", "HIGH", "LOW", "FAULT"
TO = {NORMAL: "TO_NORMAL", HIGH: "TO_HIGH", LOW: "TO_LOW", FAULT: "TO_FAULT"}

def fmt(v):
    if math.isnan(v): return "nan"
    if math.isinf(v): return "inf" if v > 0 else "-inf"
    s = "%.3f" % v
    return "0.000" if s == "-0.000" else s

class Model:
    def __init__(s, c, now):
        s.c = c; s.state = NORMAL; s.pend = None; s.start = 0; s.unack = False
        s.alarm_t = 0; s.esc = False; s.maint = False; s.last = 0.0
        s.maxnow = now; s.last_notified = NORMAL
    def target(s, v):
        c = s.c; hc = v > c["high"]; lc = v < c["low"]
        if s.state == NORMAL: return HIGH if hc else LOW if lc else None
        if s.state == HIGH: return LOW if lc else NORMAL if v < c["high"] - c["deadband"] else None
        if s.state == LOW: return HIGH if hc else NORMAL if v > c["low"] + c["deadband"] else None
        return None
    def go(s, to, now, out):
        s.state = to; s.pend = None
        if not s.maint: s.notify(now, out)
    def notify(s, now, out):
        out.append((TO[s.state], now, s.last)); s.last_notified = s.state
        if s.state in (HIGH, LOW): s.unack = True; s.alarm_t = now; s.esc = False
    def timer(s, now, out):
        if s.state != FAULT and s.pend is not None and now - s.start >= s.c["delay"]:
            s.go(s.pend, now, out)
    def escal(s, now, out):
        e = s.c["escalate"]
        if s.unack and not s.esc and not s.maint and e > 0 and now - s.alarm_t >= e:
            out.append(("ESCALATE", now, s.last)); s.esc = True
    def ok(s, now):
        if now < s.maxnow: return False
        s.maxnow = now; return True
    def sample(s, now, v, f):
        out = []
        if not s.ok(now): return out
        s.last = v
        if f or math.isnan(v):
            if s.state != FAULT: s.go(FAULT, now, out)
            s.pend = None
        else:
            if s.state == FAULT: s.go(NORMAL, now, out)
            t = s.target(v)
            if t is None: s.pend = None
            elif t != s.pend: s.pend = t; s.start = now
            s.timer(now, out)
        s.escal(now, out); return out
    def tick(s, now):
        out = []
        if not s.ok(now): return out
        s.timer(now, out); s.escal(now, out); return out
    def ack(s, now):
        out = []
        if not s.ok(now): return out
        s.timer(now, out); s.unack = False; s.escal(now, out); return out
    def maintc(s, now, on):
        out = []
        if not s.ok(now): return out
        s.timer(now, out)
        if on: s.maint = True
        elif s.maint:
            s.maint = False
            if s.state != s.last_notified: s.notify(now, out)
        s.escal(now, out); return out

def line(m, out):
    return " ".join([m.state] + ["%s@%d:%s" % (e, t, fmt(v)) for e, t, v in out])

def scenario(rng):
    c = dict(high=float(rng.choice([30, 20, 50])), low=float(rng.choice([10, 0, -5])),
             deadband=float(rng.choice([0, 0.5, 2, 5, 100])), delay=rng.choice([0, 0, 5, 10, 30]),
             escalate=rng.choice([0, 20, 60, 200]))
    now = rng.choice([0, 0, 50])
    inp = ["cfg high=%g low=%g deadband=%g delay=%d escalate=%d" % (c["high"], c["low"], c["deadband"], c["delay"], c["escalate"]),
           "init %d" % now]
    m = Model(c, now); exp = ["cfg", "NORMAL"]
    vals = [c["high"], c["low"], c["high"] - c["deadband"], c["low"] + c["deadband"],
            c["high"] + 1, c["low"] - 1, (c["high"] + c["low"]) / 2, c["high"] - c["deadband"] - 0.5,
            c["low"] + c["deadband"] + 0.5, math.inf, -math.inf, math.nan, 1e6, -1e6]
    t = now
    for _ in range(rng.randint(1, 25)):
        dt = rng.choice([0, 1, 2, 5, 10, 30, 100, -3, -50]) if rng.random() < 0.95 else 1000
        t2 = max(0, t + dt)
        if dt >= 0: t = t2
        k = rng.random()
        if k < 0.55:
            v = rng.choice(vals); f = rng.random() < 0.08
            vs = fmt(v) if not (math.isnan(v)) else "nan"
            inp.append("%s %d %s" % ("sf" if f else "s", t2, vs)); out = m.sample(t2, v, f)
        elif k < 0.75: inp.append("tick %d" % t2); out = m.tick(t2)
        elif k < 0.87: inp.append("ack %d" % t2); out = m.ack(t2)
        else:
            on = rng.random() < 0.5
            inp.append("maint %d %s" % (t2, "on" if on else "off")); out = m.maintc(t2, on)
        exp.append(line(m, out))
    return inp, exp

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    rng = random.Random(12345)
    scen = [scenario(rng) for _ in range(n)]
    stdin = []
    for i, (inp, _) in enumerate(scen):
        stdin.append("@block %d" % i); stdin.extend(inp)
    got = subprocess.run([sys.argv[2] if len(sys.argv) > 2 else "./drv"], input=("\n".join(stdin) + "\n").encode(), stdout=subprocess.PIPE).stdout.decode().split("\n")
    res, cur = {}, None
    for ln in got:
        if ln.startswith("@block "): cur = int(ln.split()[1]); res[cur] = []
        elif ln and cur is not None: res[cur].append(ln)
    bad = 0
    for i, (inp, exp) in enumerate(scen):
        if res.get(i) != exp:
            bad += 1
            if bad <= 3:
                print("MISMATCH", i)
                for a, e, g in zip(inp, exp, res.get(i, []) + [""] * 99):
                    print("  %s %-28s exp=%-45s got=%s" % ("!!" if e != g else "  ", a, e, g))
    print("%d/%d scenarios match" % (n - bad, n))
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())

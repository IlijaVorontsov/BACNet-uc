#!/usr/bin/env python3
"""Independent reference model of SPEC.md, used for randomized differential testing
against ./drv. Usage: python3 model.py [N_SCENARIOS] [SEED]"""
import math, random, struct, subprocess, sys

def f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]

NAMES = {"NORMAL": "TO_NORMAL", "HIGH": "TO_HIGH", "LOW": "TO_LOW", "FAULT": "TO_FAULT"}

class Model:
    def __init__(self, cfg, now):
        self.hi, self.lo, self.db, self.delay, self.esc = cfg
        self.maxnow = now
        self.state = "NORMAL"
        self.pending = None        # (target, start)
        self.have_sample = False
        self.value = 0.0
        self.unacked = False
        self.alarm_time = 0
        self.escalated = False
        self.maint = False
        self.last_emitted = "NORMAL"

    def _note(self, notes, now, ev):
        if not self.maint:
            notes.append((ev, now, self.value))

    def _go(self, notes, now, s):
        self.state = s
        self._announce(notes, now, s)

    def _announce(self, notes, now, s):
        if self.maint:
            return
        self._note(notes, now, NAMES[s])
        self.last_emitted = s
        if s in ("HIGH", "LOW"):
            self.unacked, self.alarm_time, self.escalated = True, now, False

    def _target(self, v):
        hc, lc = v > self.hi, v < self.lo
        if self.state == "NORMAL":
            return "HIGH" if hc else "LOW" if lc else None
        if self.state == "HIGH":
            return "LOW" if lc else "NORMAL" if v < self.hi - self.db else None
        if self.state == "LOW":
            return "HIGH" if hc else "NORMAL" if v > self.lo + self.db else None
        return None

    def _timer(self, notes, now):
        if self.pending and now - self.pending[1] >= self.delay:
            t = self.pending[0]
            self.pending = None
            self._go(notes, now, t)

    def _esc(self, notes, now):
        if (self.unacked and not self.escalated and not self.maint and self.esc > 0
                and now - self.alarm_time >= self.esc):
            self._note(notes, now, "ESCALATE")
            self.escalated = True

    def _begin(self, now):
        if now < self.maxnow:
            return False
        self.maxnow = now
        return True

    def sample(self, now, v, fault):
        notes = []
        if not self._begin(now):
            return notes
        self.value = v
        self.have_sample = True
        if fault or math.isnan(v):
            self.pending = None
            if self.state != "FAULT":
                self._go(notes, now, "FAULT")
        else:
            if self.state == "FAULT":
                self._go(notes, now, "NORMAL")
            t = self._target(v)
            if t is None:
                self.pending = None
            elif self.pending is None or self.pending[0] != t:
                self.pending = (t, now)
            self._timer(notes, now)
        self._esc(notes, now)
        return notes

    def tick(self, now):
        notes = []
        if not self._begin(now):
            return notes
        self._timer(notes, now)
        self._esc(notes, now)
        return notes

    def ack(self, now):
        notes = []
        if not self._begin(now):
            return notes
        self._timer(notes, now)
        self.unacked = False
        self._esc(notes, now)
        return notes

    def maint_set(self, now, on):
        notes = []
        if not self._begin(now):
            return notes
        self._timer(notes, now)
        if on and not self.maint:
            self.maint = True
        elif not on and self.maint:
            self.maint = False
            if self.state != self.last_emitted:
                self._announce(notes, now, self.state)
        self._esc(notes, now)
        return notes

def fmt_val(v):
    if math.isnan(v): return "nan"
    if math.isinf(v): return "inf" if v > 0 else "-inf"
    s = "%.3f" % v
    return "0.000" if s == "-0.000" else s

def fmt(state, notes):
    return " ".join([state] + ["%s@%d:%s" % (e, t, fmt_val(v)) for e, t, v in notes])

def gen(rng):
    hi = rng.choice([30.0, 30.0, 25.5, 1e30])
    lo = rng.choice([10.0, 10.0, -5.25, 29.0])
    db = rng.choice([0.0, 2.0, 2.0, 5.0, 0.5])
    delay = rng.choice([0, 0, 5, 10, 30])
    esc = rng.choice([0, 20, 50, 100])
    cfg = (f32(hi), f32(lo), f32(db), delay, esc)
    lines = ["cfg high=%r low=%r deadband=%r delay=%d escalate=%d" % (hi, lo, db, delay, esc)]
    t = rng.choice([0, 0, 7])
    lines.append("init %d" % t)
    cmds = []
    edge = [hi, lo, hi - db, lo + db, hi + 0.5, lo - 0.5, (hi + lo) / 2, 35.0, 5.0, 20.0, 27.0, 13.0]
    for _ in range(rng.randint(5, 40)):
        dt = rng.choice([0, 1, 2, 5, 5, 10, 10, 20, 30, 60, 120])
        if rng.random() < 0.05:
            dt = -rng.randint(1, 10)
        t = max(0, t + dt)
        r = rng.random()
        if r < 0.55:
            v = rng.choice(edge) if rng.random() < 0.7 else rng.uniform(-10, 50)
            v = round(v, 3)
            vs = repr(v)
            if rng.random() < 0.05: vs, v = "nan", float("nan")
            elif rng.random() < 0.04: vs, v = "inf", float("inf")
            elif rng.random() < 0.04: vs, v = "-inf", float("-inf")
            kind = "sf" if rng.random() < 0.08 else "s"
            cmds.append((kind, t, vs, f32(v)))
        elif r < 0.75:
            cmds.append(("tick", t))
        elif r < 0.87:
            cmds.append(("ack", t))
        else:
            cmds.append(("maint", t, rng.choice(["on", "off"])))
    return cfg, lines, cmds

def run(n, seed):
    rng = random.Random(seed)
    inp, exp = [], []
    for i in range(n):
        cfg, lines, cmds = gen(rng)
        inp.append("@block %d" % i)
        exp.append("@block %d" % i)
        inp.extend(lines)
        exp.append("cfg")
        m = Model(cfg, int(lines[1].split()[1]))
        exp.append("NORMAL")
        for c in cmds:
            if c[0] in ("s", "sf"):
                inp.append("%s %d %s" % (c[0], c[1], c[2]))
                notes = m.sample(c[1], c[3], c[0] == "sf")
            elif c[0] == "tick":
                inp.append("tick %d" % c[1]); notes = m.tick(c[1])
            elif c[0] == "ack":
                inp.append("ack %d" % c[1]); notes = m.ack(c[1])
            else:
                inp.append("maint %d %s" % (c[1], c[2])); notes = m.maint_set(c[1], c[2] == "on")
            exp.append(fmt(m.state, notes))
    drv = sys.argv[3] if len(sys.argv) > 3 else "./drv"
    out = subprocess.run([drv], input=("\n".join(inp) + "\n").encode(), stdout=subprocess.PIPE).stdout.decode().split("\n")
    out = [o for o in out if o != ""]
    # map outputs to blocks for readable diffs
    bad = 0
    gi = ii = 0
    blocks_in = {}
    cur = None
    for l in inp:
        if l.startswith("@block"): cur = l; blocks_in[cur] = []
        else: blocks_in[cur].append(l)
    cur = None
    for e, g in zip(exp, out):
        if e.startswith("@block"): cur = e; idx = 0; continue
        if e != g:
            bad += 1
            if bad <= 5:
                print("MISMATCH in", cur)
                print("\n".join("   " + x for x in blocks_in[cur]))
                print("   line %d expected %r got %r" % (idx, e, g))
        idx += 1
    if len(exp) != len(out):
        print("length mismatch", len(exp), len(out)); bad += 1
    print("%d lines checked, %d mismatches" % (len(exp), bad))
    return bad

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    sys.exit(1 if run(n, seed) else 0)

#!/usr/bin/env python3
"""Differential fuzz: independent model of the alarm.c contract vs ./drv (dev aid)."""
import math, random, struct, subprocess, sys

def f32(x):
    return struct.unpack('f', struct.pack('f', x))[0]

N, H, L, F = 0, 1, 2, 3
SN = {0: 'NORMAL', 1: 'HIGH', 2: 'LOW', 3: 'FAULT'}
EV = {0: 'TO_NORMAL', 1: 'TO_HIGH', 2: 'TO_LOW', 3: 'TO_FAULT', 4: 'ESCALATE'}

def fmt(v):
    if math.isnan(v): return 'nan'
    if math.isinf(v): return 'inf' if v > 0 else '-inf'
    s = '%.3f' % v
    return '0.000' if s == '-0.000' else s

class M:
    def __init__(s, cfg, now):
        s.hi, s.lo, s.db, s.delay, s.esc = cfg
        s.state = N; s.ln = N; s.pend = None; s.ps = 0
        s.unacked = False; s.escd = False; s.maint = False
        s.at = 0; s.last = now; s.val = 0.0
    def tgt(s, v):
        if s.state == N:
            if v > s.hi: return H
            if v < s.lo: return L
        elif s.state == H:
            if v < s.lo: return L
            if v < f32(s.hi - s.db): return N
        elif s.state == L:
            if v > s.hi: return H
            if v > f32(s.lo + s.db): return N
        return None
    def notify(s, now, out):
        out.append((s.state, now, s.val)); s.ln = s.state
        if s.state in (H, L):
            s.unacked = True; s.at = now; s.escd = False
    def trans(s, to, now, out):
        s.state = to; s.pend = None
        if not s.maint: s.notify(now, out)
    def timer(s, now, out):
        if s.state == F or s.pend is None: return
        if now - s.ps >= s.delay: s.trans(s.pend, now, out)
    def escal(s, now, out):
        if s.unacked and not s.escd and not s.maint and s.esc > 0 and now - s.at >= s.esc:
            out.append((4, now, s.val)); s.escd = True
    def acc(s, now):
        if now < s.last: return False
        s.last = now; return True
    def sample(s, now, v, fault):
        out = []
        if not s.acc(now): return out
        s.val = v
        if fault or math.isnan(v):
            if s.state != F: s.trans(F, now, out)
            s.pend = None
        else:
            if s.state == F: s.trans(N, now, out)
            t = s.tgt(v)
            if t is None: s.pend = None
            elif t != s.pend: s.pend = t; s.ps = now
            s.timer(now, out)
        s.escal(now, out); return out
    def tick(s, now):
        out = []
        if not s.acc(now): return out
        s.timer(now, out); s.escal(now, out); return out
    def ack(s, now):
        out = []
        if not s.acc(now): return out
        s.timer(now, out); s.unacked = False; s.escal(now, out); return out
    def mnt(s, now, on):
        out = []
        if not s.acc(now): return out
        s.timer(now, out)
        if on: s.maint = True
        elif s.maint:
            s.maint = False
            if s.state != s.ln: s.notify(now, out)
        s.escal(now, out); return out

def line(m, out):
    return ' '.join([SN[m.state]] + ['%s@%d:%s' % (EV[e], t, fmt(v)) for e, t, v in out])

def gen(rng):
    hi = rng.choice([30, 30.5, 100]); lo = rng.choice([10, -5, 0.25]); db = rng.choice([0, 2, 0.5])
    delay = rng.choice([0, 0, 5, 10, 30]); esc = rng.choice([0, 20, 50, 100])
    cfg = (f32(hi), f32(lo), f32(db), delay, esc)
    inp = ['cfg high=%r low=%r deadband=%r delay=%d escalate=%d' % (hi, lo, db, delay, esc)]
    t0 = rng.choice([0, 5, 100, 4294967000])
    inp.append('init %d' % t0)
    m = M(cfg, t0); exp = ['cfg', 'NORMAL']
    t = t0
    vals = [hi, lo, hi - db, lo + db, hi + 1, lo - 1, (hi + lo) / 2, hi - db - 0.5, lo + db + 0.5,
            float('inf'), float('-inf'), float('nan'), hi + 50, lo - 50]
    for _ in range(rng.randint(1, 40)):
        dt = rng.choice([0, 1, 3, 5, 10, 20, 50, -3, -50])
        tt = min(max(t + dt, 0), 4294967295)
        if dt >= 0: t = tt
        k = rng.random()
        if k < 0.55:
            v = f32(rng.choice(vals)); fault = rng.random() < 0.1
            vs = 'nan' if math.isnan(v) else ('inf' if v == math.inf else ('-inf' if v == -math.inf else repr(v)))
            inp.append('%s %d %s' % ('sf' if fault else 's', tt, vs)); out = m.sample(tt, v, fault)
        elif k < 0.75:
            inp.append('tick %d' % tt); out = m.tick(tt)
        elif k < 0.87:
            inp.append('ack %d' % tt); out = m.ack(tt)
        else:
            on = rng.random() < 0.5
            inp.append('maint %d %s' % (tt, 'on' if on else 'off')); out = m.mnt(tt, on)
        assert len(out) <= 4
        exp.append(line(m, out))
    return inp, exp

def main():
    rng = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    cases = [gen(rng) for _ in range(n)]
    data = []
    for i, (inp, _) in enumerate(cases):
        data.append('@block %d' % i); data.extend(inp)
    r = subprocess.run([sys.argv[3] if len(sys.argv) > 3 else './drv'], input=('\n'.join(data) + '\n').encode(), stdout=subprocess.PIPE)
    got = {}; cur = None
    for ln in r.stdout.decode().split('\n'):
        if ln.startswith('@block '): cur = int(ln.split()[1]); got[cur] = []
        elif ln and cur is not None: got[cur].append(ln.rstrip())
    bad = 0
    for i, (inp, exp) in enumerate(cases):
        if got.get(i) != exp:
            bad += 1
            if bad <= 3:
                print('MISMATCH case', i)
                for a, e, g in zip(inp, exp, got.get(i, []) + [''] * 99):
                    print('  %-45s exp=%-40s got=%s%s' % (a, e, g, '' if e == g else '   <<<'))
    print('%d/%d cases agree' % (n - bad, n))
    return 1 if bad else 0

sys.exit(main())

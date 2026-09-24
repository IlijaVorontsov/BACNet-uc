#!/usr/bin/env python3
"""Independent Python model of SPEC.md + random differential test against ./drv."""
import math, random, struct, subprocess, sys

def f32(x):
    return struct.unpack('f', struct.pack('f', x))[0]

N, H, L, F = 'NORMAL', 'HIGH', 'LOW', 'FAULT'

class Model:
    def __init__(s, cfg, now):
        s.hi, s.lo, s.db, s.delay, s.esc = cfg
        s.maxnow = now; s.state = N; s.lastnote = N; s.pend = None; s.pstart = 0
        s.val = 0.0; s.has = False; s.unacked = False; s.atime = 0; s.escd = False; s.maint = False
        s.out = []
    def note(s, ev):
        s.out.append((ev, s.now, s.val))
    def trans(s, to):
        s.state = to
        if not s.maint:
            s.note('TO_' + to); s.lastnote = to
            if to in (H, L): s.unacked = True; s.atime = s.now; s.escd = False
    def target(s, v):
        hc, lc = v > s.hi, v < s.lo
        if s.state == N: return H if hc else (L if lc else None)
        if s.state == H: return L if lc else (N if v < f32(s.hi - s.db) else None)
        if s.state == L: return H if hc else (N if v > f32(s.lo + s.db) else None)
        return None
    def timer(s):
        if s.pend is not None and s.state != F and s.now - s.pstart >= s.delay:
            t = s.pend; s.pend = None; s.trans(t)
    def esc_check(s):
        if s.unacked and not s.escd and not s.maint and s.esc > 0 and s.now - s.atime >= s.esc:
            s.escd = True; s.note('ESCALATE')
    def begin(s, now):
        s.out = []
        if now < s.maxnow: return False
        s.maxnow = now; s.now = now; return True
    def sample(s, now, v, fault):
        if not s.begin(now): return []
        s.val = v; s.has = True
        if fault or math.isnan(v):
            s.pend = None
            if s.state != F: s.trans(F)
        else:
            if s.state == F: s.pend = None; s.trans(N)
            t = s.target(v)
            if t is None: s.pend = None
            elif s.pend != t: s.pend = t; s.pstart = now
            s.timer()
        s.esc_check(); return s.out
    def tick(s, now):
        if not s.begin(now): return []
        s.timer(); s.esc_check(); return s.out
    def ack(s, now):
        if not s.begin(now): return []
        s.timer(); s.unacked = False; s.esc_check(); return s.out
    def maintc(s, now, on):
        if not s.begin(now): return []
        s.timer()
        if on and not s.maint: s.maint = True
        elif not on and s.maint:
            s.maint = False
            if s.state != s.lastnote:
                st = s.state; s.note('TO_' + st); s.lastnote = st
                if st in (H, L): s.unacked = True; s.atime = now; s.escd = False
        s.esc_check(); return s.out

def fmt(v):
    if math.isnan(v): return 'nan'
    if math.isinf(v): return 'inf' if v > 0 else '-inf'
    r = '%.3f' % v
    return '0.000' if r == '-0.000' else r

def render(m, notes):
    return ' '.join([m.state] + ['%s@%d:%s' % (e, t, fmt(v)) for e, t, v in notes])

VALS = ['5', '9.5', '10', '10.5', '11.9', '12', '12.5', '20', '27.5', '28', '28.5', '30', '30.5', '35',
        'nan', 'inf', '-inf', '-3', '100']
def pval(s):
    return float('nan') if s == 'nan' else f32(float(s))

def gen(rng):
    hi = rng.choice(['30', '30', '30', '20', '12'])
    lo = rng.choice(['10', '10', '10', '28', '30'])  # sometimes inverted / equal
    db = rng.choice(['0', '2', '2', '0.5', '25'])
    delay = rng.choice([0, 0, 1, 5, 10, 30])
    esc = rng.choice([0, 1, 20, 50, 100])
    cfgline = f'cfg high={hi} low={lo} deadband={db} delay={delay} escalate={esc}'
    cfg = (pval(hi), pval(lo), pval(db), delay, esc)
    t = rng.randint(0, 5)
    cmds = [cfgline, f'init {t}']
    for _ in range(rng.randint(1, 40)):
        dt = rng.choice([0, 0, 1, 2, 5, 10, 30, 60, -3])
        t2 = max(0, t + dt)
        if dt >= 0: t = t2
        r = rng.random()
        if r < 0.55: cmds.append(f's {t2} {rng.choice(VALS)}')
        elif r < 0.62: cmds.append(f'sf {t2} {rng.choice(VALS)}')
        elif r < 0.78: cmds.append(f'tick {t2}')
        elif r < 0.88: cmds.append(f'ack {t2}')
        else: cmds.append(f'maint {t2} {rng.choice(["on", "off"])}')
    return cmds, cfg

def expect(cmds, cfg):
    exp = ['cfg']; m = None
    for c in cmds[1:]:
        p = c.split()
        if p[0] == 'init': m = Model(cfg, int(p[1])); exp.append('NORMAL'); continue
        t = int(p[1])
        if p[0] in ('s', 'sf'): n = m.sample(t, pval(p[2]), p[0] == 'sf')
        elif p[0] == 'tick': n = m.tick(t)
        elif p[0] == 'ack': n = m.ack(t)
        else: n = m.maintc(t, p[2] == 'on')
        exp.append(render(m, n))
    return exp

def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    rng = random.Random(seed)
    with open('fuzz.vec', 'w') as f:
        for i in range(count):
            cmds, cfg = gen(rng)
            f.write(f'### fuzz{i}\n' + '\n'.join(cmds) + '\n---\n' + '\n'.join(expect(cmds, cfg)) + '\n')
main()

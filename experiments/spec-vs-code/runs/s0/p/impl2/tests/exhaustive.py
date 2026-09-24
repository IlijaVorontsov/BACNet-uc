#!/usr/bin/env python3
"""Exhaustive sweeps against the model in diff.py: every 0..2-byte decoder input,
a large slice of 3/5-byte inputs, every date/time field value, capacity sweeps."""
import os, subprocess, sys, itertools
sys.argv = ['diff.py', '0', '0']
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'diff.py')).read().replace('\nmain()\n', '\n')
g = {'__name__': 'm', '__file__': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'diff.py')}
exec(src, g)
cmds, exps = [], []
def add(c, e): cmds.append(c); exps.append(e)
inputs = [b''] + [bytes([a]) for a in range(256)] + [bytes([a, b]) for a in range(256) for b in range(256)]
sel = [0, 1, 3, 4, 5, 0x0f, 0x10, 0x7f, 0x80, 0xfd, 0xfe, 0xff]
inputs += [bytes([a, b, c]) for a in range(256) for b in range(256) for c in sel]
inputs += [bytes([a, b, c, d, e]) for a in range(256) for b in sel for c in sel for d in (0, 0x80) for e in (0, 0xff)]
for b in inputs:
    h = b.hex() or '-'
    add('dec_tag ' + h, g['s_dec_tag'](b)); add('dec_app ' + h, g['s_dec_app'](b))
for f in range(256):
    for pos in range(4):
        a = [1, 1, 1, 1]; a[pos] = f
        y = 2000
        add('enc_date %d %d %d %d' % (y, *a[1:]), g['encres'](g['m_date'](y, *a[1:]), 1024)) if pos else None
        add('enc_time %d %d %d %d' % tuple(a), g['encres'](g['m_time'](*a), 1024))
for y in range(0, 65536, 1):
    if y < 1880 or y > 2170:
        if y % 97: continue
    add('enc_date %d 1 1 1' % y, g['encres'](g['m_date'](y, 1, 1, 1), 1024))
# capacity sweeps
for cmd, full in [('enc_null', g['tv'](0, False, b'')), ('enc_bool 1', b'\x11'),
                  ('enc_unsigned 4294967295', g['tv'](2, False, g['minu'](4294967295))),
                  ('enc_signed -2147483648', g['tv'](3, False, g['mins'](-2**31))),
                  ('enc_real 3f800000', g['m_real'](0x3f800000)),
                  ('enc_octets rep:ab:300', g['tv'](6, False, b'\xab' * 300)),
                  ('enc_octets rep:ab:70000', g['tv'](6, False, b'\xab' * 70000)),
                  ('enc_str rep:41:253', g['tv'](7, False, b'\x00' + b'A' * 253)),
                  ('enc_bits rep:1:2017', g['m_bits']([1] * 2017)),
                  ('enc_date 2024 2 29 4', g['m_date'](2024, 2, 29, 4)),
                  ('enc_time 1 2 3 4', g['m_time'](1, 2, 3, 4)),
                  ('enc_oid 1023 4194303', g['m_oid'](1023, 4194303)),
                  ('enc_ctx_unsigned 200 65536', g['tv'](200, True, g['minu'](65536))),
                  ('enc_ctx_bool 254 1', g['tv'](254, True, b'\x01')),
                  ('enc_open 254', g['m_openclose'](254, 6)), ('enc_close 7', g['m_openclose'](7, 7))]:
    L = len(full)
    caps = list(range(0, min(L + 3, 40))) + list(range(max(0, L - 10), L + 3))
    for c in sorted(set(caps)):
        add('%s @%d' % (cmd, c), g['encres'](full, c))
out = subprocess.run([g['DRV']], input=('\n'.join(cmds) + '\n').encode(), stdout=subprocess.PIPE, check=True).stdout.decode().split('\n')
bad = sum(1 for e, o in zip(exps, out) if e != o)
for c, e, o in zip(cmds, exps, out):
    if e != o:
        print('MISMATCH', c[:100], '| exp', e, '| got', o); break
print('%d/%d ok' % (len(cmds) - bad, len(cmds)), 'lines', len(out) - 1)
sys.exit(1 if bad or len(out) - 1 != len(cmds) else 0)

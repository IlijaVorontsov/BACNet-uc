#!/usr/bin/env python3
"""Differential test: independent Python model of SPEC.md vs ./drv.
Generates random commands (encoders with random capacities, decoders on random
and mutated inputs), runs drv once, compares every line."""
import random, struct, subprocess, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
DRV = os.path.join(HERE, '..', 'drv')

def fnv(b):
    h = 1469598103934665603
    for x in b:
        h ^= x; h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h

def pbytes(b):
    if not b: return '-'
    if len(b) <= 64: return b.hex()
    return b[:16].hex() + '..%016x/%d' % (fnv(b), len(b))

def pbits(data, nbits):
    if nbits == 0: return '-'
    if nbits <= 256:
        return ''.join('1' if (data[i // 8] >> (7 - i % 8)) & 1 else '0' for i in range(nbits))
    nb = (nbits + 7) // 8
    return '%dbits..%016x' % (nbits, fnv(data[:nb - 1]) ^ (data[nb - 1] >> (nb * 8 - nbits)))

# ---------------- model encoders (return bytes or None) ----------------
def hdr(tag, ctx, clen):
    if tag > 254: return None
    lvt = clen if clen <= 4 else 5
    out = bytes([((15 if tag >= 15 else tag) << 4) | (8 if ctx else 0) | lvt])
    if tag >= 15: out += bytes([tag])
    if clen > 4:
        if clen <= 253: out += bytes([clen])
        elif clen <= 65535: out += bytes([254]) + clen.to_bytes(2, 'big')
        else: out += bytes([255]) + clen.to_bytes(4, 'big')
    return out

def tv(tag, ctx, content):
    h = hdr(tag, ctx, len(content))
    return None if h is None else h + content

def minu(v):
    n = 1
    while v >= 1 << (8 * n): n += 1
    return v.to_bytes(n, 'big')

def mins(v):
    n = 1
    while not (-(1 << (8 * n - 1)) <= v < (1 << (8 * n - 1))): n += 1
    return (v & ((1 << (8 * n)) - 1)).to_bytes(n, 'big')

def m_real(bits):
    if (bits & 0x7F800000) == 0x7F800000 and bits & 0x7FFFFF: return None
    return tv(4, False, bits.to_bytes(4, 'big'))

def m_bits(bits):
    if any(b > 1 for b in bits): return None
    n = len(bits)
    unused = (8 - n % 8) % 8
    out = bytearray((n + 7) // 8)
    for i, b in enumerate(bits):
        if b: out[i // 8] |= 0x80 >> (i % 8)
    return tv(8, False, bytes([unused]) + bytes(out))

def m_date(y, m, d, w):
    if y == 0xFFFF: yo = 255
    elif 1900 <= y <= 2154: yo = y - 1900
    else: return None
    if not (1 <= m <= 14 or m == 255): return None
    if not (1 <= d <= 34 or d == 255): return None
    if not (1 <= w <= 7 or w == 255): return None
    return tv(10, False, bytes([yo, m, d, w]))

def m_time(h, mi, s, hu):
    for v, mx in ((h, 23), (mi, 59), (s, 59), (hu, 99)):
        if not (v <= mx or v == 255): return None
    return tv(11, False, bytes([h, mi, s, hu]))

def m_oid(t, i):
    if t > 1023 or i > 4194303: return None
    return tv(12, False, ((t << 22) | i).to_bytes(4, 'big'))

def m_openclose(tag, lvt):
    if tag > 254: return None
    out = bytes([((15 if tag >= 15 else tag) << 4) | 8 | lvt])
    return out + (bytes([tag]) if tag >= 15 else b'')

def encres(b, cap):
    if b is None or len(b) > cap: return 'ERR'
    return 'OK ' + pbytes(b)

# ---------------- model decoders ----------------
def m_dec_tag(b):
    if not b: return None
    t = b[0] >> 4; ctx = bool(b[0] & 8); lvt = b[0] & 7; p = 1
    if t == 15:
        if p >= len(b) or b[p] == 255: return None
        t = b[p]; p += 1
    if lvt in (6, 7):
        if not ctx: return None
        return (p, t, ctx, 'open' if lvt == 6 else 'close', None)
    if lvt == 5:
        if p >= len(b): return None
        x = b[p]; p += 1
        if x <= 253: L = x
        else:
            n = 2 if x == 254 else 4
            if len(b) - p < n: return None
            L = int.from_bytes(b[p:p + n], 'big'); p += n
        return (p, t, ctx, None, L)
    return (p, t, ctx, None, lvt)

def s_dec_tag(b):
    r = m_dec_tag(b)
    if r is None: return 'ERR'
    p, t, ctx, oc, L = r
    s = 'OK hl=%d tag=%d %s' % (p, t, 'ctx' if ctx else 'app')
    return s + (' ' + oc if oc else ' lvt=%d' % L)

def s_dec_app(b):
    r = m_dec_tag(b)
    if r is None: return 'ERR'
    p, t, ctx, oc, L = r
    if ctx: return 'ERR'
    raw = b[0] & 7
    if t == 0:
        return 'OK n=%d null' % p if raw == 0 else 'ERR'
    if t == 1:
        return 'OK n=%d bool %d' % (p, raw) if raw <= 1 else 'ERR'
    if t in (5, 13, 14) or t >= 15: return 'ERR'
    if L > len(b) - p: return 'ERR'
    c = b[p:p + L]; n = p + L
    if t in (2, 9, 3):
        if not 1 <= L <= 4: return 'ERR'
        v = int.from_bytes(c, 'big', signed=(t == 3))
        return 'OK n=%d %s %d' % (n, {2: 'u', 9: 'enum', 3: 'i'}[t], v)
    if t == 4:
        return 'OK n=%d r %s' % (n, c.hex()) if L == 4 else 'ERR'
    if t == 6: return 'OK n=%d oct %s' % (n, pbytes(c))
    if t == 7:
        if L < 1: return 'ERR'
        return 'OK n=%d str cs=%d %s' % (n, c[0], pbytes(c[1:]))
    if t == 8:
        if L < 1 or c[0] > 7 or (L == 1 and c[0] != 0): return 'ERR'
        return 'OK n=%d bits %s' % (n, pbits(c[1:], (L - 1) * 8 - c[0]))
    if L != 4: return 'ERR'
    if t == 10:
        return 'OK n=%d date %d %d %d %d' % (n, 65535 if c[0] == 255 else 1900 + c[0], c[1], c[2], c[3])
    if t == 11: return 'OK n=%d time %d %d %d %d' % (n, *c)
    u = int.from_bytes(c, 'big')
    return 'OK n=%d oid %d %d' % (n, u >> 22, u & 0x3FFFFF)

# ---------------- generators ----------------
R = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 1)

def rand_len():
    return R.choice([0, 1, 2, 3, 4, 5, 6, 252, 253, 254, 255, 256, 65534, 65535, 65536, 65537,
                     R.randint(0, 300), R.randint(0, 70000)])

def bytes_arg(b):
    if not b: return '-'
    # compress runs with rep:
    parts = []; i = 0
    while i < len(b):
        j = i
        while j < len(b) and b[j] == b[i]: j += 1
        if j - i > 8: parts.append('rep:%02x:%d' % (b[i], j - i))
        else: parts.append(b[i:j].hex())
        i = j
    # merge adjacent hex segments
    out = []
    for p in parts:
        if out and not p.startswith('rep') and not out[-1].startswith('rep'): out[-1] += p
        else: out.append(p)
    return '+'.join(out)

def rand_bytes(n):
    if n > 400:
        return bytes([R.randrange(256)]) * (n - 3) + bytes(R.randrange(256) for _ in range(3))
    return bytes(R.randrange(256) for _ in range(n))

def cap_for(expected):
    if expected is None: return R.choice([None, 0, 1, 5, 1024])
    L = len(expected)
    return R.choice([None, L, L, L - 1, L + 1, 0, max(0, L - R.randint(1, 5))])

def enc_case():
    k = R.randrange(15)
    if k == 0: cmd, exp = 'enc_null', tv(0, False, b'')
    elif k == 1:
        v = R.randrange(2); cmd, exp = 'enc_bool %d' % v, bytes([0x10 | v])
    elif k in (2, 3):
        v = R.choice([R.randrange(1 << 32), R.randrange(1 << R.randint(1, 32)), 0, 255, 256, 65535, 65536, (1 << 24) - 1, 1 << 24])
        name, tag = (('enc_unsigned', 2), ('enc_enum', 9))[k - 2]
        cmd, exp = '%s %d' % (name, v), tv(tag, False, minu(v))
    elif k == 4:
        v = R.choice([R.randrange(-2**31, 2**31), R.randrange(-(1 << R.randint(0, 31)), 1 << R.randint(0, 31)), -128, 127, -129, 128, -32768, 32767, -32769, 32768, -2**31, 2**31 - 1])
        cmd, exp = 'enc_signed %d' % v, tv(3, False, mins(v))
    elif k == 5:
        bits = R.choice([R.randrange(1 << 32), 0x7F800000 | R.randrange(1 << 23), 0xFF800000 | R.randrange(1 << 23), 0x7F800000, 0xFF800000, 0x80000000, 0, struct.unpack('>I', struct.pack('>f', R.uniform(-1e30, 1e30)))[0]])
        cmd, exp = 'enc_real %08x' % bits, m_real(bits)
    elif k == 6:
        b = rand_bytes(rand_len()); cmd, exp = 'enc_octets ' + bytes_arg(b), tv(6, False, b)
    elif k == 7:
        b = rand_bytes(min(rand_len(), 70000)); cmd, exp = 'enc_str ' + bytes_arg(b), tv(7, False, b'\x00' + b)
    elif k == 8:
        n = rand_len()
        if n > 400:
            d = R.choice('01'); bits = [int(d)] * n
            arg = 'rep:%s:%d' % (d, n)
        else:
            bits = [R.randrange(2) for _ in range(n)]
            if n and R.random() < 0.2: bits[R.randrange(n)] = R.randint(2, 9)
            arg = ''.join(map(str, bits)) or '-'
        cmd, exp = 'enc_bits ' + arg, m_bits(bits)
    elif k == 9:
        y = R.choice([65535, R.randint(1890, 2160), R.randrange(65536), 1900, 2154, 1899, 2155, 255])
        f = lambda lo, hi: R.choice([255, R.randint(lo, hi), R.randrange(256), 0, hi + 1])
        m, d, w = f(1, 14), f(1, 34), f(1, 7)
        cmd, exp = 'enc_date %d %d %d %d' % (y, m, d, w), m_date(y, m, d, w)
    elif k == 10:
        f = lambda hi: R.choice([255, R.randint(0, hi), R.randrange(256), hi + 1, 254])
        a = (f(23), f(59), f(59), f(99))
        cmd, exp = 'enc_time %d %d %d %d' % a, m_time(*a)
    elif k == 11:
        t = R.choice([R.randrange(1024), 1023, 1024, R.randrange(65536)])
        i = R.choice([R.randrange(1 << 22), 4194303, 4194304, R.randrange(1 << 32)])
        cmd, exp = 'enc_oid %d %d' % (t, i), m_oid(t, i)
    elif k == 12:
        t = R.choice([R.randrange(256), 14, 15, 254, 255]); v = R.randrange(1 << R.randint(1, 32))
        cmd, exp = 'enc_ctx_unsigned %d %d' % (t, v), tv(t, True, minu(v))
    elif k == 13:
        t = R.choice([R.randrange(256), 14, 15, 254, 255]); v = R.randrange(2)
        cmd, exp = 'enc_ctx_bool %d %d' % (t, v), tv(t, True, bytes([v]))
    else:
        t = R.choice([R.randrange(256), 14, 15, 254, 255]); o = R.randrange(2)
        cmd, exp = '%s %d' % (('enc_open', 'enc_close')[o], t), m_openclose(t, 6 + o)
    cap = cap_for(exp)
    if cap is None: cap = 1024 if exp is None or len(exp) <= 1024 else 80000
    return cmd + ' @%d' % cap, encres(exp, cap)

def rand_valid_encoding():
    cmd, exp = enc_case()
    if exp.startswith('OK ') and '..' not in exp:
        return bytes.fromhex(exp[3:]) if exp[3:] != '-' else b''
    return rand_bytes(R.randint(0, 8))

def dec_input():
    k = R.randrange(4)
    if k == 0: b = rand_bytes(R.randint(0, 10))
    elif k == 1:
        b = bytearray(rand_valid_encoding())
        for _ in range(R.randint(0, 2)):
            if b: b[R.randrange(len(b))] = R.randrange(256)
        if b and R.random() < 0.3: b = b[:R.randrange(len(b))]
        if R.random() < 0.3: b += rand_bytes(R.randint(1, 3))
        b = bytes(b)
    elif k == 2:
        # structured header + content
        first = R.randrange(256)
        b = bytes([first])
        if first >> 4 == 15: b += bytes([R.choice([R.randrange(256), 255, 15, 3])])
        if first & 7 == 5:
            x = R.choice([R.randrange(254), 254, 255, 0, 4, 5])
            b += bytes([x])
            if x == 254: b += R.randrange(0, 300).to_bytes(2, 'big')
            if x == 255: b += R.randrange(0, 300).to_bytes(4, 'big')
        b += rand_bytes(R.randint(0, 10))
    else:
        b = rand_valid_encoding()
    return b

def main():
    N = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
    cmds, exps = [], []
    for _ in range(N):
        r = R.randrange(3)
        if r == 0:
            c, e = enc_case()
        else:
            b = dec_input()
            if r == 1: c, e = 'dec_tag ' + bytes_arg(b), s_dec_tag(b)
            else: c, e = 'dec_app ' + bytes_arg(b), s_dec_app(b)
        cmds.append(c); exps.append(e)
    out = subprocess.run([DRV], input=('\n'.join(cmds) + '\n').encode(), stdout=subprocess.PIPE, check=True).stdout.decode().split('\n')
    bad = 0
    for c, e, g in zip(cmds, exps, out):
        if e != g:
            bad += 1
            if bad <= 20: print('MISMATCH', c[:200], '\n  exp', e, '\n  got', g)
    if len(out) - 1 != len(cmds): print('line count mismatch', len(out), len(cmds)); bad += 1
    import collections
    st = collections.Counter((c.split()[0], e.split()[0]) for c, e in zip(cmds, exps))
    if os.environ.get('STATS'): print(sorted(st.items()))
    print('%d/%d ok' % (N - bad, N))
    sys.exit(1 if bad else 0)

main()

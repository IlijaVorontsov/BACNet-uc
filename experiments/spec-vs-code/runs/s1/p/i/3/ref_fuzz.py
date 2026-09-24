#!/usr/bin/env python3
"""Differential fuzz: compares ./drv against an independent Python model of
ASHRAE 135 clause 20.2 (encode + decode + round trip).  Usage: ref_fuzz.py [N] [seed]"""
import random, struct, subprocess, sys

def fnv(b):
    h = 1469598103934665603
    for x in b:
        h ^= x; h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h

def pb(b):
    if not b: return "-"
    if len(b) <= 64: return b.hex()
    return b[:16].hex() + "..%016x/%d" % (fnv(b), len(b))

def hdr(tag, ctx, n):
    t = (min(tag, 15) << 4) | (8 if ctx else 0)
    ext = bytes([tag]) if tag >= 15 else b""
    if n < 5: return bytes([t | n]) + ext
    if n <= 253: return bytes([t | 5]) + ext + bytes([n])
    if n <= 65535: return bytes([t | 5]) + ext + b"\xfe" + n.to_bytes(2, "big")
    return bytes([t | 5]) + ext + b"\xff" + n.to_bytes(4, "big")

def prim(tag, content, ctx=False): return hdr(tag, ctx, len(content)) + content
def uenc(v): return v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")
def senc(v):
    for n in (1, 2, 3, 4):
        if -(1 << (8 * n - 1)) <= v < (1 << (8 * n - 1)): return (v & ((1 << 8 * n) - 1)).to_bytes(n, "big")

def utf8ok(b):
    try: b.decode("utf-8", "strict"); return True
    except UnicodeDecodeError: return False

def bits_to_bytes(bits):
    nb = (len(bits) + 7) // 8
    out = bytearray(nb)
    for i, d in enumerate(bits):
        if d: out[i // 8] |= 0x80 >> (i % 8)
    return bytes([nb * 8 - len(bits)]) + bytes(out)

def bitdigits(data, nbits):
    if nbits == 0: return "-"
    if nbits <= 256: return "".join("1" if (data[i // 8] >> (7 - i % 8)) & 1 else "0" for i in range(nbits))
    nb = (nbits + 7) // 8
    return "%dbits..%016x" % (nbits, fnv(data[:nb - 1]) ^ (data[nb - 1] >> (nb * 8 - nbits)))

def ok(b, cap):
    return "OK " + pb(b) if len(b) <= cap else "ERR"

def rand_bytes(r, n): return bytes(r.getrandbits(8) for _ in range(n))
def rand_len(r):
    return r.choice([0, 1, 3, 4, 5, 6, 252, 253, 254, 255, r.randrange(0, 300), 65534, 65535, 65536, 65537, r.randrange(0, 70000)])

def utf8_text(r, n):
    out = []
    while sum(len(c) for c in out) < n:
        cp = r.choice([r.randrange(0, 0x80), r.randrange(0x80, 0x800), r.randrange(0x800, 0xD800), r.randrange(0x10000, 0x110000)])
        out.append(chr(cp).encode())
    b = b"".join(out)
    return b

def dec_model(b):
    """Independent decoder model -> expected driver line."""
    if not b: return "ERR"
    t = b[0] >> 4; ctx = bool(b[0] & 8); lvt = b[0] & 7; p = 1
    if ctx: return "ERR"
    if t == 15: return "ERR"  # app tags >= 15 reserved (and ext tag needs >=2 bytes anyway)
    if t == 1:
        return "OK n=1 bool %d" % lvt if lvt <= 1 else "ERR"
    if lvt in (6, 7): return "ERR"
    if lvt == 5:
        if len(b) < 2: return "ERR"
        e = b[1]; p = 2
        if e < 5: return "ERR"
        if e <= 253: L = e
        elif e == 254:
            if len(b) < 4: return "ERR"
            L = int.from_bytes(b[2:4], "big"); p = 4
            if L < 254: return "ERR"
        else:
            if len(b) < 6: return "ERR"
            L = int.from_bytes(b[2:6], "big"); p = 6
            if L < 65536: return "ERR"
    else:
        L = lvt
    if p + L > len(b): return "ERR"
    c = b[p:p + L]; n = p + L
    if t == 0: return "OK n=%d null" % n if L == 0 else "ERR"
    if t in (2, 9):
        if not 1 <= L <= 4 or (L > 1 and c[0] == 0): return "ERR"
        return "OK n=%d %s %d" % (n, "u" if t == 2 else "enum", int.from_bytes(c, "big"))
    if t == 3:
        if not 1 <= L <= 4: return "ERR"
        if L > 1 and ((c[0] == 0 and c[1] < 0x80) or (c[0] == 0xff and c[1] >= 0x80)): return "ERR"
        return "OK n=%d i %d" % (n, int.from_bytes(c, "big", signed=True))
    if t == 4: return "OK n=%d r %s" % (n, c.hex()) if L == 4 else "ERR"
    if t == 6: return "OK n=%d oct %s" % (n, pb(c))
    if t == 7:
        if L < 1: return "ERR"
        if c[0] == 0 and not utf8ok(c[1:]): return "ERR"
        return "OK n=%d str cs=%d %s" % (n, c[0], pb(c[1:]))
    if t == 8:
        if L < 1 or c[0] > 7 or (L == 1 and c[0]): return "ERR"
        return "OK n=%d bits %s" % (n, bitdigits(c[1:], (L - 1) * 8 - c[0]))
    if t == 10:
        if L != 4: return "ERR"
        y, m, d, w = c
        if not (1 <= m <= 14 or m == 255) or not (1 <= d <= 34 or d == 255) or not (1 <= w <= 7 or w == 255): return "ERR"
        return "OK n=%d date %d %d %d %d" % (n, 65535 if y == 255 else 1900 + y, m, d, w)
    if t == 11:
        if L != 4: return "ERR"
        h, mi, s, hs = c
        if (h > 23 and h != 255) or (mi > 59 and mi != 255) or (s > 59 and s != 255) or (hs > 99 and hs != 255): return "ERR"
        return "OK n=%d time %d %d %d %d" % (n, h, mi, s, hs)
    if t == 12:
        if L != 4: return "ERR"
        u = int.from_bytes(c, "big")
        return "OK n=%d oid %d %d" % (n, u >> 22, u & 0x3FFFFF)
    return "ERR"

def gen(r):
    """Returns (command, expected, encoded-bytes-or-None)."""
    k = r.randrange(14)
    cap = r.choice([1024, 1024, None])
    enc = None
    if k == 0:
        v = r.choice([r.getrandbits(32), r.getrandbits(r.randrange(1, 33)), 0, 255, 256])
        enc = prim(2, uenc(v)); cmd = "enc_unsigned %d" % v
    elif k == 1:
        v = r.getrandbits(r.randrange(1, 33)) - (1 << 31) if r.random() < .3 else r.randrange(-(1 << r.randrange(1, 32)), 1 << r.randrange(1, 32))
        v = max(-(1 << 31), min((1 << 31) - 1, v))
        enc = prim(3, senc(v)); cmd = "enc_signed %d" % v
    elif k == 2:
        u = r.getrandbits(32); enc = prim(4, u.to_bytes(4, "big")); cmd = "enc_real %08x" % u
    elif k == 3:
        d = rand_bytes(r, rand_len(r)); enc = prim(6, d)
        cmd = "enc_octets " + (d.hex() if d else "-") if len(d) < 1000 else "enc_octets rep:%02x:%d" % (d[0], len(d))
        if len(d) >= 1000: d = bytes([d[0]]) * len(d); enc = prim(6, d)
    elif k == 4:
        n = rand_len(r)
        if n > 1000:
            d = b"A" * n; cmd = "enc_str rep:41:%d" % n
        else:
            d = utf8_text(r, n) if r.random() < .7 else rand_bytes(r, n); cmd = "enc_str " + (d.hex() if d else "-")
        enc = prim(7, b"\x00" + d) if utf8ok(d) else None
    elif k == 5:
        n = r.choice([0, 1, 7, 8, 9, 31, 32, 33, 39, 40, 41, 255, 256, 257, 2020, 2031, 2032, 2033, r.randrange(0, 3000), 2039, 2040, 2041, 79999, 80000])
        if n > 2000:
            d = r.randrange(0, 2); bits = [d] * n; cmd = "enc_bits rep:%d:%d" % (d, n)
        else:
            bits = [r.choice([0, 1, 1, r.randrange(10)]) for _ in range(n)]; cmd = "enc_bits " + ("".join(map(str, bits)) or "-")
        enc = prim(8, bits_to_bytes(bits))
    elif k == 6:
        v = r.getrandbits(r.randrange(1, 33)); enc = prim(9, uenc(v)); cmd = "enc_enum %d" % v
    elif k == 7:
        y = r.choice([r.randrange(1890, 2170), 65535, r.randrange(0, 65536)]); m = r.choice([r.randrange(0, 16), 255, r.randrange(256)])
        d = r.choice([r.randrange(0, 36), 255, r.randrange(256)]); w = r.choice([r.randrange(0, 9), 255, r.randrange(256)])
        cmd = "enc_date %d %d %d %d" % (y, m, d, w)
        valid = (1900 <= y <= 2154 or y == 65535) and (1 <= m <= 14 or m == 255) and (1 <= d <= 34 or d == 255) and (1 <= w <= 7 or w == 255)
        enc = prim(10, bytes([255 if y == 65535 else (y - 1900) & 0xff, m, d, w])) if valid else None
    elif k == 8:
        f = [r.choice([r.randrange(0, 26), 255, r.randrange(256)]), r.choice([r.randrange(0, 62), 255]), r.choice([r.randrange(0, 62), 255]), r.choice([r.randrange(0, 102), 255, r.randrange(256)])]
        cmd = "enc_time %d %d %d %d" % tuple(f)
        valid = (f[0] <= 23 or f[0] == 255) and (f[1] <= 59 or f[1] == 255) and (f[2] <= 59 or f[2] == 255) and (f[3] <= 99 or f[3] == 255)
        enc = prim(11, bytes(f)) if valid else None
    elif k == 9:
        t = r.choice([r.randrange(1024), r.randrange(1020, 1030), r.randrange(65536)]); i = r.choice([r.getrandbits(22), r.randrange(4194300, 4194310), r.getrandbits(32)])
        cmd = "enc_oid %d %d" % (t, i)
        enc = prim(12, ((t << 22) | i).to_bytes(4, "big")) if t < 1024 and i < (1 << 22) else None
    elif k == 10:
        t = r.choice([r.randrange(15), 15, r.randrange(256), 254, 255]); v = r.getrandbits(r.randrange(1, 33))
        cmd = "enc_ctx_unsigned %d %d" % (t, v); enc = prim(t, uenc(v), True) if t < 255 else None
    elif k == 11:
        t = r.choice([r.randrange(15), r.randrange(256), 255]); v = r.randrange(2)
        cmd = "enc_ctx_bool %d %d" % (t, v); enc = prim(t, bytes([v]), True) if t < 255 else None
    elif k == 12:
        t = r.choice([r.randrange(15), r.randrange(256), 255]); o = r.randrange(2)
        cmd = "%s %d" % ("enc_open" if o else "enc_close", t)
        enc = (bytes([0xfe if o else 0xff, t]) if t >= 15 else bytes([(t << 4) | (0x0e if o else 0x0f)])) if t < 255 else None
    else:
        # random / mutated decode input
        b = bytearray(rand_bytes(r, r.randrange(0, 12)))
        if b and r.random() < .5: b[0] = (b[0] & 0xf7)
        b = bytes(b)
        return "dec_app " + (b.hex() or "-"), dec_model(b), None
    if cap is None and enc is not None:
        cap = max(0, len(enc) + r.choice([-2, -1, 0, 1]))
    if cap is None: cap = r.randrange(0, 8)
    exp = "ERR" if enc is None else ok(enc, cap)
    return cmd + ("" if cap == 1024 else " @%d" % cap), exp, enc

def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    r = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    cmds, exps = [], []
    for _ in range(N):
        c, e, enc = gen(r)
        cmds.append(c); exps.append(e)
        if enc is not None and len(enc) < 60000:
            # round trip through the decoder (model computes the expected text)
            cmds.append("dec_app " + (enc.hex() if len(enc) < 3000 else "%s+rep:%02x:%d" % (enc[:8].hex(), enc[8], len(enc) - 8) if len(set(enc[8:])) == 1 else enc.hex()))
            exps.append(dec_model(enc))
    inp = "\n".join(cmds) + "\n"
    out = subprocess.run(["./drv"], input=inp.encode(), stdout=subprocess.PIPE, check=True).stdout.decode().split("\n")
    bad = 0
    for c, e, g in zip(cmds, exps, out):
        if e != g:
            bad += 1
            if bad <= 15: print("MISMATCH", c[:120], "\n  exp", e, "\n  got", g)
    print("%d/%d agree" % (len(cmds) - bad, len(cmds)))
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())

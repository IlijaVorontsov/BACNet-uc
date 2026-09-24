#!/usr/bin/env python3
"""Developer test: differential fuzzing of drv against an independent Python
model of ASHRAE 135 clause 20.2.  Writes fuzz.vec and runs it.
Usage: python3 refmodel_fuzz.py [seed] [count] [drv]"""
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
    first = 0x08 if ctx else 0
    ext = b""
    if tag >= 15: first |= 0xF0; ext = bytes([tag])
    else: first |= tag << 4
    if n < 5: first |= n; ln = b""
    else:
        first |= 5
        if n < 254: ln = bytes([n])
        elif n < 65536: ln = bytes([254]) + n.to_bytes(2, "big")
        else: ln = bytes([255]) + n.to_bytes(4, "big")
    return bytes([first]) + ext + ln

def ubytes(v):
    n = 1
    while v >= 1 << (8 * n): n += 1
    return v.to_bytes(n, "big")

def sbytes(v):
    for n in range(1, 5):
        if -(1 << (8 * n - 1)) <= v < (1 << (8 * n - 1)):
            return v.to_bytes(n, "big", signed=True)

def utf8ok(b):
    try:
        b.decode("utf-8", "strict"); return True
    except UnicodeDecodeError:
        return False

def enc_result(cmd, cap):
    c = cmd[0]; a = cmd[1:]
    tag = None
    if c == "enc_null": out = hdr(0, 0, 0)
    elif c == "enc_bool": out = bytes([0x10 | a[0]])
    elif c == "enc_unsigned": b = ubytes(a[0]); out = hdr(2, 0, len(b)) + b
    elif c == "enc_enum": b = ubytes(a[0]); out = hdr(9, 0, len(b)) + b
    elif c == "enc_signed": b = sbytes(a[0]); out = hdr(3, 0, len(b)) + b
    elif c == "enc_real": out = hdr(4, 0, 4) + a[0]
    elif c == "enc_octets": out = hdr(6, 0, len(a[0])) + a[0]
    elif c == "enc_str":
        if not utf8ok(a[0]): return None
        out = hdr(7, 0, len(a[0]) + 1) + b"\0" + a[0]
    elif c == "enc_bits":
        bits = a[0]; nb = (len(bits) + 7) // 8
        body = bytearray(nb)
        for i, d in enumerate(bits):
            if d: body[i // 8] |= 0x80 >> (i % 8)
        out = hdr(8, 0, nb + 1) + bytes([(8 - len(bits) % 8) % 8]) + bytes(body)
    elif c == "enc_date":
        y, m, d, w = a
        if y == 0xFFFF: yb = 255
        elif 1900 <= y <= 2154: yb = y - 1900
        else: return None
        if not (1 <= m <= 14 or m == 255): return None
        if not (1 <= d <= 34 or d == 255): return None
        if not (1 <= w <= 7 or w == 255): return None
        out = hdr(10, 0, 4) + bytes([yb, m, d, w])
    elif c == "enc_time":
        h, mi, s, hu = a
        lim = [23, 59, 59, 99]
        if any(not (x <= l or x == 255) for x, l in zip(a, lim)): return None
        out = hdr(11, 0, 4) + bytes(a)
    elif c == "enc_oid":
        t, i = a
        if t > 1023 or i > 0x3FFFFF: return None
        out = hdr(12, 0, 4) + ((t << 22) | i).to_bytes(4, "big")
    elif c == "enc_ctx_unsigned":
        if a[0] == 255: return None
        b = ubytes(a[1]); out = hdr(a[0], 1, len(b)) + b
    elif c == "enc_ctx_bool":
        if a[0] == 255: return None
        out = hdr(a[0], 1, 1) + bytes([a[1]])
    elif c in ("enc_open", "enc_close"):
        if a[0] == 255: return None
        h = hdr(a[0], 1, 0)
        out = bytes([h[0] | (6 if c == "enc_open" else 7)]) + h[1:]
    if len(out) > cap: return None
    return out

def fmt_arg(x, c):
    if isinstance(x, bytes):
        if c == "enc_real": return x.hex()
        return x.hex() if x else "-"
    if isinstance(x, list): return "".join(str(d) for d in x) if x else "-"
    return str(x)

def dec_tag(b):
    if not b: return None
    b0 = b[0]; ctx = bool(b0 & 8); tag = b0 >> 4; lvt = b0 & 7; i = 1
    if tag == 15:
        if len(b) < 2 or b[1] == 255: return None
        tag = b[1]; i = 2
    if lvt in (6, 7):
        if not ctx: return None
        return (i, tag, ctx, "open" if lvt == 6 else "close", 0)
    if not ctx and tag == 1:
        if lvt > 1: return None
        return (i, tag, ctx, None, lvt)
    if lvt == 5:
        if len(b) < i + 1: return None
        e = b[i]; i += 1
        if e < 254: lvt = e
        elif e == 254:
            if len(b) < i + 2: return None
            lvt = int.from_bytes(b[i:i+2], "big"); i += 2
        else:
            if len(b) < i + 4: return None
            lvt = int.from_bytes(b[i:i+4], "big"); i += 4
    return (i, tag, ctx, None, lvt)

def dec_tag_str(b):
    r = dec_tag(b)
    if r is None: return "ERR"
    hl, tag, ctx, oc, lvt = r
    s = "OK hl=%d tag=%d %s" % (hl, tag, "ctx" if ctx else "app")
    return s + (" " + oc if oc else " lvt=%d" % lvt)

def bitdigits(data, nbits):
    if nbits == 0: return "-"
    if nbits <= 256:
        return "".join("1" if (data[i // 8] >> (7 - i % 8)) & 1 else "0" for i in range(nbits))
    nb = (nbits + 7) // 8
    return "%dbits..%016x" % (nbits, fnv(data[:nb-1]) ^ (data[nb-1] >> (nb * 8 - nbits)))

def dec_app(b):
    r = dec_tag(b)
    if r is None: return "ERR"
    hl, tag, ctx, oc, lvt = r
    if ctx: return "ERR"
    if tag == 1: return "OK n=%d bool %d" % (hl, lvt)
    if lvt > len(b) - hl: return "ERR"
    c = b[hl:hl+lvt]; n = hl + lvt
    p = "OK n=%d " % n
    if tag == 0: return p + "null" if lvt == 0 else "ERR"
    if tag in (2, 9):
        if lvt == 0: return "ERR"
        v = int.from_bytes(c, "big")
        if v > 0xFFFFFFFF: return "ERR"
        return p + ("u %d" if tag == 2 else "enum %d") % v
    if tag == 3:
        if lvt == 0: return "ERR"
        v = int.from_bytes(c, "big", signed=True)
        if not -2**31 <= v < 2**31: return "ERR"
        return p + "i %d" % v
    if tag == 4: return p + "r " + c.hex() if lvt == 4 else "ERR"
    if tag == 6: return p + "oct " + pb(c)
    if tag == 7: return p + "str cs=%d %s" % (c[0], pb(c[1:])) if lvt >= 1 else "ERR"
    if tag == 8:
        if lvt < 1 or c[0] > 7 or (lvt == 1 and c[0]): return "ERR"
        return p + "bits " + bitdigits(c[1:], (lvt - 1) * 8 - c[0])
    if tag in (10, 11, 12):
        if lvt != 4: return "ERR"
        if tag == 10: return p + "date %d %d %d %d" % ((65535 if c[0] == 255 else 1900 + c[0]), c[1], c[2], c[3])
        if tag == 11: return p + "time %d %d %d %d" % tuple(c)
        v = int.from_bytes(c, "big"); return p + "oid %d %d" % (v >> 22, v & 0x3FFFFF)
    return "ERR"

def rand_u32(r):
    k = r.choice([0, 1, 2, 3, 4])
    if k == 0: return r.choice([0, 1, 255, 256, 65535, 65536, 0xFFFFFF, 0x1000000, 0xFFFFFFFF])
    return r.getrandbits(8 * k)

def rand_len(r):
    return r.choice([0, 1, 2, 3, 4, 5, 6, 252, 253, 254, 255, 256, 1000, 65534, 65535, 65536, 65537, r.randrange(0, 70000)])

def rand_bytes(r, n):
    if r.random() < 0.3: return bytes([r.randrange(256)]) * n
    return bytes(r.getrandbits(8) for _ in range(n))

def rand_utf8(r, n):
    if r.random() < 0.3:
        return rand_bytes(r, min(n, 8))
    s = "".join(chr(r.choice([r.randrange(0, 0x80), r.randrange(0x80, 0x800), r.randrange(0xE000, 0x10000), r.randrange(0x10000, 0x110000)])) for _ in range(min(n, 3000)))
    return s.encode("utf-8")

def big_arg(b):
    # compress runs so the command line fits in the driver's 400000-char line buffer
    return b.hex() if b else "-"

def gen(r):
    c = r.choice(["enc_null", "enc_bool", "enc_unsigned", "enc_enum", "enc_signed", "enc_real",
                  "enc_octets", "enc_str", "enc_bits", "enc_date", "enc_time", "enc_oid",
                  "enc_ctx_unsigned", "enc_ctx_bool", "enc_open", "enc_close"])
    if c == "enc_null": a = []
    elif c == "enc_bool": a = [r.randrange(2)]
    elif c in ("enc_unsigned", "enc_enum"): a = [rand_u32(r)]
    elif c == "enc_signed":
        v = rand_u32(r); a = [v - (1 << 32) if v >= 1 << 31 else v]
        if r.random() < 0.5: a = [-a[0] if a[0] != -2**31 else a[0]]
    elif c == "enc_real": a = [rand_bytes(r, 4)]
    elif c == "enc_octets": a = [rand_bytes(r, rand_len(r))]
    elif c == "enc_str": a = [rand_utf8(r, rand_len(r))]
    elif c == "enc_bits":
        n = rand_len(r)
        a = [[r.choice([0, 1, 1, 0, r.randrange(10)]) for _ in range(n)]] if r.random() < 0.5 else [[r.randrange(2)] * n]
    elif c == "enc_date":
        a = [r.choice([65535, 1899, 1900, 2154, 2155, r.randrange(1900, 2155), r.randrange(0, 65536)])] + \
            [r.choice([0, 1, 12, 13, 14, 15, 32, 33, 34, 35, 254, 255, r.randrange(256)]) for _ in range(3)]
    elif c == "enc_time":
        a = [r.choice([0, 23, 24, 59, 60, 99, 100, 254, 255, r.randrange(256)]) for _ in range(4)]
    elif c == "enc_oid":
        a = [r.choice([0, 1023, 1024, 65535, r.randrange(1024)]), r.choice([0, 0x3FFFFF, 0x400000, 0xFFFFFFFF, r.randrange(0x400000)])]
    elif c == "enc_ctx_unsigned": a = [r.choice([0, 14, 15, 254, 255, r.randrange(256)]), rand_u32(r)]
    elif c == "enc_ctx_bool": a = [r.choice([0, 14, 15, 254, 255, r.randrange(256)]), r.randrange(2)]
    else: a = [r.choice([0, 14, 15, 254, 255, r.randrange(256)])]
    return c, a

def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    drv = sys.argv[3] if len(sys.argv) > 3 else "./drv"
    r = random.Random(seed)
    lines = ["# generated by refmodel_fuzz.py seed=%d" % seed]
    for k in range(count):
        c, a = gen(r)
        full = enc_result([c] + a, 1 << 30)
        # capacity: default, exact, one short, zero, or random
        choice = r.random()
        if full is not None and choice < 0.3: cap = len(full)
        elif full is not None and choice < 0.55: cap = max(len(full) - 1, 0)
        elif choice < 0.6: cap = 0
        elif choice < 0.75: cap = r.randrange(0, 80000)
        else: cap = None
        capv = 1024 if cap is None else cap
        exp = enc_result([c] + a, capv)
        cmd = " ".join([c] + [fmt_arg(x, c) for x in a] + ([] if cap is None else ["@%d" % cap]))
        if len(cmd) > 390000: continue
        ins = [cmd]; outs = ["ERR" if exp is None else "OK " + pb(exp)]
        # decode what we produced (full encoding), plus a truncated prefix
        if full is not None and c not in ("enc_ctx_unsigned", "enc_ctx_bool", "enc_open", "enc_close"):
            enc = full + (rand_bytes(r, r.randrange(3)) if r.random() < 0.3 else b"")
            if len(enc) * 2 < 390000:
                ins.append("dec_app " + big_arg(enc)); outs.append(dec_app(enc))
                cut = r.randrange(0, len(full)) if len(full) else 0
                ins.append("dec_app " + big_arg(full[:cut])); outs.append(dec_app(full[:cut]))
        if full is not None and len(full) * 2 < 390000:
            ins.append("dec_tag " + big_arg(full)); outs.append(dec_tag_str(full))
        blk = ["### enc-%d" % k] + ins + ["---"] + outs
        lines += blk
    # random garbage decoding
    for k in range(count):
        n = r.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 20])
        b = bytes(r.getrandbits(8) for _ in range(n))
        if r.random() < 0.5 and n:
            b = bytes([r.choice([0x00, 0x10, 0x11, 0x12, 0x21, 0x25, 0x31, 0x35, 0x44, 0x45, 0x55, 0x65, 0x71, 0x75, 0x81, 0x82, 0x85, 0x91, 0xA4, 0xB4, 0xC4, 0xC5, 0xD1, 0xE1, 0xF1, 0xF5, 0xFE, 0xFF, 0x0E, 0x0F, 0x26, 0x27])]) + b[1:]
        lines += ["### garbage-%d" % k, "dec_app " + (b.hex() or "-"), "dec_tag " + (b.hex() or "-"), "---", dec_app(b), dec_tag_str(b)]
    open("fuzz.vec", "w").write("\n".join(lines) + "\n")
    sys.exit(subprocess.call([sys.executable, "run_vectors.py", "--drv", drv, "fuzz.vec"]))

main()

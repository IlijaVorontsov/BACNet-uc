#!/usr/bin/env python3
"""Differential fuzz: independent Python model of clause 20.2 vs ./drv.

Usage: python3 fuzz_model.py [--drv ./drv] [--n 20000] [--seed 1]
Generates random commands (valid, boundary and malformed), feeds them to the
driver in one batch and compares every output line with the model.
"""
import argparse, random, struct, subprocess, sys


def fnv(b):
    h = 1469598103934665603
    for x in b:
        h ^= x
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def pb(b):
    if not b:
        return "-"
    if len(b) <= 64:
        return b.hex()
    return b[:16].hex() + "..%016x/%d" % (fnv(b), len(b))


def hdr(tag, ctx, length):
    cls = 0x08 if ctx else 0
    lvt = length if length <= 4 else 5
    out = bytes([(tag << 4 if tag < 15 else 0xF0) | cls | lvt])
    if tag >= 15:
        out += bytes([tag])
    if length > 4:
        if length <= 253:
            out += bytes([length])
        elif length <= 0xFFFF:
            out += b"\xfe" + length.to_bytes(2, "big")
        else:
            out += b"\xff" + length.to_bytes(4, "big")
    return out


def uoct(v):
    return v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")


def soct(v):
    for n in (1, 2, 3, 4):
        if -(1 << (8 * n - 1)) <= v < (1 << (8 * n - 1)):
            return v.to_bytes(n, "big", signed=True)


def enc_result(b, cap):
    if b is None or len(b) > cap:
        return "ERR"
    return "OK " + pb(b)


def m_enc(cmd, args, cap):
    if cmd == "enc_null":
        b = b"\x00"
    elif cmd == "enc_bool":
        b = bytes([0x10 | args[0]])
    elif cmd == "enc_unsigned":
        o = uoct(args[0]); b = hdr(2, 0, len(o)) + o
    elif cmd == "enc_enum":
        o = uoct(args[0]); b = hdr(9, 0, len(o)) + o
    elif cmd == "enc_signed":
        o = soct(args[0]); b = hdr(3, 0, len(o)) + o
    elif cmd == "enc_real":
        b = b"\x44" + args[0].to_bytes(4, "big")
    elif cmd == "enc_octets":
        b = hdr(6, 0, len(args[0])) + args[0]
    elif cmd == "enc_str":
        b = hdr(7, 0, len(args[0]) + 1) + b"\x00" + args[0]
    elif cmd == "enc_bits":
        bits = args[0]
        nb = (len(bits) + 7) // 8
        data = bytearray(nb)
        for i, x in enumerate(bits):
            if x:
                data[i // 8] |= 0x80 >> (i % 8)
        b = hdr(8, 0, nb + 1) + bytes([(8 - len(bits) % 8) % 8]) + bytes(data)
    elif cmd == "enc_date":
        y, m, d, w = args
        ok = (y == 65535 or 1900 <= y <= 2154) and (1 <= m <= 14 or m == 255) \
            and (1 <= d <= 34 or d == 255) and (1 <= w <= 7 or w == 255)
        b = bytes([0xA4, 255 if y == 65535 else (y - 1900) & 0xFF, m, d, w]) if ok else None
    elif cmd == "enc_time":
        h, mi, s, hu = args
        ok = all(v == 255 or v <= lim for v, lim in ((h, 23), (mi, 59), (s, 59), (hu, 99)))
        b = bytes([0xB4, h, mi, s, hu]) if ok else None
    elif cmd == "enc_oid":
        t, i = args
        b = b"\xc4" + ((t << 22) | i).to_bytes(4, "big") if t < 1024 and i < (1 << 22) else None
    elif cmd == "enc_ctx_unsigned":
        t, v = args
        o = uoct(v); b = hdr(t, 1, len(o)) + o if t != 255 else None
    elif cmd == "enc_ctx_bool":
        t, v = args
        b = hdr(t, 1, 1) + bytes([v]) if t != 255 else None
    elif cmd in ("enc_open", "enc_close"):
        t = args[0]
        lvt = 6 if cmd == "enc_open" else 7
        b = (bytes([(t << 4) | 8 | lvt]) if t < 15 else bytes([0xF8 | lvt, t])) if t != 255 else None
    return enc_result(b, cap)


def m_dec_tag(b):
    if not b:
        return None
    p = 1
    tag, ctx, lvt = b[0] >> 4, bool(b[0] & 8), b[0] & 7
    if tag == 15:
        if p >= len(b) or b[p] == 255:
            return None
        tag = b[p]; p += 1
    res = dict(tag=tag, ctx=ctx, open=False, close=False, lvt=0)
    if not ctx and tag == 1:
        if lvt > 1:
            return None
        res["lvt"] = lvt
    elif lvt in (6, 7):
        if not ctx:
            return None
        res["open" if lvt == 6 else "close"] = True
    elif lvt == 5:
        if p >= len(b):
            return None
        e = b[p]; p += 1
        n = {254: 2, 255: 4}.get(e, 0)
        if n:
            if len(b) - p < n:
                return None
            res["lvt"] = int.from_bytes(b[p:p + n], "big"); p += n
        else:
            res["lvt"] = e
    else:
        res["lvt"] = lvt
    res["hl"] = p
    return res


def fmt_tag(t):
    if t is None:
        return "ERR"
    s = "OK hl=%d tag=%d %s" % (t["hl"], t["tag"], "ctx" if t["ctx"] else "app")
    if t["open"]:
        s += " open"
    if t["close"]:
        s += " close"
    if not t["open"] and not t["close"]:
        s += " lvt=%d" % t["lvt"]
    return s


def m_dec_app(b):
    t = m_dec_tag(b)
    if t is None or t["ctx"] or t["open"] or t["close"]:
        return "ERR"
    hl, tag, n = t["hl"], t["tag"], t["lvt"]
    if tag == 1:
        return "OK n=%d bool %d" % (hl, t["lvt"])
    if n > len(b) - hl:
        return "ERR"
    c = b[hl:hl + n]
    pre = "OK n=%d " % (hl + n)
    if tag == 0:
        return pre + "null" if n == 0 else "ERR"
    if tag in (2, 9):
        if not 1 <= n <= 4:
            return "ERR"
        return pre + ("u %d" if tag == 2 else "enum %d") % int.from_bytes(c, "big")
    if tag == 3:
        if not 1 <= n <= 4:
            return "ERR"
        return pre + "i %d" % int.from_bytes(c, "big", signed=True)
    if tag == 4:
        return pre + "r " + c.hex() if n == 4 else "ERR"
    if tag == 6:
        return pre + "oct " + pb(c)
    if tag == 7:
        return pre + "str cs=%d %s" % (c[0], pb(c[1:])) if n >= 1 else "ERR"
    if tag == 8:
        if n < 1 or c[0] > 7 or (n == 1 and c[0]):
            return "ERR"
        nbits = (n - 1) * 8 - c[0]
        d = c[1:]
        if nbits == 0:
            s = "-"
        elif nbits <= 256:
            s = "".join("1" if (d[i // 8] >> (7 - i % 8)) & 1 else "0" for i in range(nbits))
        else:
            nb = (nbits + 7) // 8
            s = "%dbits..%016x" % (nbits, fnv(d[:nb - 1]) ^ (d[nb - 1] >> (nb * 8 - nbits)))
        return pre + "bits " + s
    if tag == 10:
        if n != 4:
            return "ERR"
        return pre + "date %d %d %d %d" % (65535 if c[0] == 255 else 1900 + c[0], c[1], c[2], c[3])
    if tag == 11:
        return pre + "time %d %d %d %d" % tuple(c) if n == 4 else "ERR"
    if tag == 12:
        if n != 4:
            return "ERR"
        v = int.from_bytes(c, "big")
        return pre + "oid %d %d" % (v >> 22, v & 0x3FFFFF)
    return "ERR"


def rnd_u32(r):
    return r.choice([0, 1, 0xFF, 0x100, 0xFFFF, 0x10000, 0xFFFFFF, 0x1000000, 0xFFFFFFFF,
                     r.getrandbits(r.choice([8, 16, 24, 32]))])


def rnd_len(r):
    return r.choice([0, 1, 3, 4, 5, 6, 252, 253, 254, 255, 256, 1000, 65534, 65535, 65536, 65537,
                     r.randrange(0, 300)])


def gen(r):
    """Returns (command_line, expected_output)."""
    k = r.randrange(17)
    cap = None
    capstr = ""
    if r.random() < 0.25:
        cap = r.randrange(0, 12) if r.random() < 0.7 else r.randrange(0, 80000)
        capstr = " @%d" % cap
    c = 1024 if cap is None else cap
    if k == 0:
        return "enc_null" + capstr, m_enc("enc_null", [], c)
    if k == 1:
        v = r.randrange(2); return "enc_bool %d%s" % (v, capstr), m_enc("enc_bool", [v], c)
    if k == 2:
        cmd = r.choice(["enc_unsigned", "enc_enum"]); v = rnd_u32(r)
        return "%s %d%s" % (cmd, v, capstr), m_enc(cmd, [v], c)
    if k == 3:
        v = r.choice([0, -1, 127, 128, -128, -129, 32767, 32768, -32768, -32769, 8388607, 8388608,
                      -8388608, -8388609, 2**31 - 1, -2**31, r.randrange(-2**31, 2**31)])
        return "enc_signed %d%s" % (v, capstr), m_enc("enc_signed", [v], c)
    if k == 4:
        v = r.getrandbits(32); return "enc_real %08x%s" % (v, capstr), m_enc("enc_real", [v], c)
    if k in (5, 6):
        n = rnd_len(r); byte = r.randrange(256)
        if n > 70000:
            n = 70000
        data = bytes([byte]) * n
        arg = "rep:%02x:%d" % (byte, n) if n else "-"
        if 0 < n < 20 and r.random() < 0.5:
            data = bytes(r.randrange(256) for _ in range(n)); arg = data.hex()
        cmd = "enc_octets" if k == 5 else "enc_str"
        return "%s %s%s" % (cmd, arg, capstr), m_enc(cmd, [data], c)
    if k == 7:
        n = r.choice([0, 1, 7, 8, 9, 15, 16, 17, r.randrange(0, 100), r.randrange(0, 5000)])
        bits = [r.choice([0, 1, 1, 0, r.randrange(10)]) for _ in range(n)]
        arg = "".join(map(str, bits)) if n else "-"
        return "enc_bits %s%s" % (arg, capstr), m_enc("enc_bits", [bits], c)
    if k == 8:
        a = [r.choice([65535, 1900, 2154, 2155, 1899, 0, r.randrange(1900, 2155), r.randrange(65536)]),
             r.choice([0, 1, 12, 13, 14, 15, 255, r.randrange(256)]),
             r.choice([0, 1, 31, 32, 33, 34, 35, 255, r.randrange(256)]),
             r.choice([0, 1, 7, 8, 255, r.randrange(256)])]
        return "enc_date %d %d %d %d%s" % (*a, capstr), m_enc("enc_date", a, c)
    if k == 9:
        a = [r.choice([0, 23, 24, 255, r.randrange(256)]), r.choice([0, 59, 60, 255, r.randrange(256)]),
             r.choice([0, 59, 60, 255, r.randrange(256)]), r.choice([0, 99, 100, 255, r.randrange(256)])]
        return "enc_time %d %d %d %d%s" % (*a, capstr), m_enc("enc_time", a, c)
    if k == 10:
        a = [r.choice([0, 1023, 1024, 65535, r.randrange(1024)]),
             r.choice([0, 4194303, 4194304, 0xFFFFFFFF, r.randrange(1 << 22)])]
        return "enc_oid %d %d%s" % (*a, capstr), m_enc("enc_oid", a, c)
    if k == 11:
        t = r.choice([0, 14, 15, 254, 255, r.randrange(256)])
        cmd = r.choice(["enc_ctx_unsigned", "enc_ctx_bool", "enc_open", "enc_close"])
        if cmd == "enc_ctx_unsigned":
            v = rnd_u32(r); return "%s %d %d%s" % (cmd, t, v, capstr), m_enc(cmd, [t, v], c)
        if cmd == "enc_ctx_bool":
            v = r.randrange(2); return "%s %d %d%s" % (cmd, t, v, capstr), m_enc(cmd, [t, v], c)
        return "%s %d%s" % (cmd, t, capstr), m_enc(cmd, [t], c)
    # decoders: structured-random byte strings
    if k in (12, 13):
        b = bytes(r.randrange(256) for _ in range(r.randrange(0, 12)))
    elif k == 14:
        # well-formed-ish header + random body with length perturbation
        tag = r.choice(list(range(16)) + [r.randrange(256)])
        n = rnd_len(r) % 400
        body = bytes(r.randrange(256) for _ in range(max(0, n + r.choice([-1, 0, 0, 0, 1]))))
        b = hdr(tag, r.random() < 0.2, n) + body
    else:
        tag = r.randrange(13)
        n = r.choice([0, 1, 2, 3, 4, 5, r.randrange(10)])
        b = hdr(tag, False, n) + bytes(r.randrange(256) for _ in range(n))
    arg = b.hex() if b else "-"
    if r.random() < 0.3:
        return "dec_tag " + arg, fmt_tag(m_dec_tag(b))
    return "dec_app " + arg, m_dec_app(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drv", default="./drv")
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    r = random.Random(a.seed)
    cases = [gen(r) for _ in range(a.n)]
    inp = ("\n".join(c for c, _ in cases) + "\n").encode()
    out = subprocess.run([a.drv], input=inp, stdout=subprocess.PIPE, check=True).stdout.decode().split("\n")
    bad = 0
    for (cmd, exp), got in zip(cases, out):
        if got.rstrip() != exp:
            bad += 1
            if bad <= 20:
                print("MISMATCH", cmd[:120], "\n   exp:", exp, "\n   got:", got)
    print("%d/%d agree" % (len(cases) - bad, len(cases)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

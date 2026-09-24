#!/usr/bin/env python3
"""Generate differential test vectors from an independent Python model of clause 20.2.

Usage: python3 tests/gen_vectors.py [SEED] > tests/generated.vec
       python3 run_vectors.py tests/generated.vec

The model below is written from the standard, not translated from bacapp.c, and
formats results exactly like driver.c does.  It encodes the same policy choices
documented at the top of bacapp.c (strict encoders, decoders that reject
malformed data but accept non-canonical length/integer forms).
"""
import random
import struct
import sys

UNSPEC = 255


# ---------------------------------------------------------------- formatting
def fnv1a(b):
    h = 1469598103934665603
    for x in b:
        h ^= x
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def fmt_bytes(b):
    if not b:
        return "-"
    if len(b) <= 64:
        return b.hex()
    return f"{b[:16].hex()}..{fnv1a(b):016x}/{len(b)}"


def fmt_bits(data, nbits):
    if nbits == 0:
        return "-"
    if nbits <= 256:
        return "".join("1" if (data[i // 8] >> (7 - i % 8)) & 1 else "0" for i in range(nbits))
    nb = (nbits + 7) // 8
    return f"{nbits}bits..{fnv1a(data[:nb - 1]) ^ (data[nb - 1] >> (nb * 8 - nbits)):016x}"


def arg_bytes(b):
    """Compact driver argument for a byte string (uses rep: for runs)."""
    if not b:
        return "-"
    segs, i = [], 0
    while i < len(b):
        j = i
        while j < len(b) and b[j] == b[i]:
            j += 1
        if j - i >= 8:
            segs.append(f"rep:{b[i]:02x}:{j - i}")
        else:
            if segs and not segs[-1].startswith("rep:"):
                segs[-1] += b[i:j].hex()
            else:
                segs.append(b[i:j].hex())
        i = j
    return "+".join(segs)


def arg_bits(bits):
    if not bits:
        return "-"
    segs, i = [], 0
    while i < len(bits):
        j = i
        while j < len(bits) and bits[j] == bits[i]:
            j += 1
        if j - i >= 8:
            segs.append(f"rep:{bits[i]}:{j - i}")
        else:
            s = "".join(str(x) for x in bits[i:j])
            if segs and not segs[-1].startswith("rep:"):
                segs[-1] += s
            else:
                segs.append(s)
        i = j
    return "+".join(segs)


# ---------------------------------------------------------------- encoder model
def header(tag, ctx, length):
    cls = 0x08 if ctx else 0
    ext_tag = tag >= 15
    first_tag = 15 if ext_tag else tag
    if length <= 4:
        out = bytes([(first_tag << 4) | cls | length])
    else:
        out = bytes([(first_tag << 4) | cls | 5])
    if ext_tag:
        out += bytes([tag])
    if length > 4:
        if length <= 253:
            out += bytes([length])
        elif length <= 0xFFFF:
            out += b"\xfe" + length.to_bytes(2, "big")
        else:
            out += b"\xff" + length.to_bytes(4, "big")
    return out


def prim(tag, content, ctx=False):
    if tag == 255:
        return None
    return header(tag, ctx, len(content)) + content


def uint_content(v):
    n = max(1, (v.bit_length() + 7) // 8)
    return v.to_bytes(n, "big")


def sint_content(v):
    for n in range(1, 5):
        if -(1 << (8 * n - 1)) <= v < (1 << (8 * n - 1)):
            return v.to_bytes(n, "big", signed=True)
    raise ValueError


def date_ok(m, d, w):
    return (m == UNSPEC or 1 <= m <= 14) and (d == UNSPEC or 1 <= d <= 34) and (w == UNSPEC or 1 <= w <= 7)


def time_ok(h, mi, s, hu):
    return (h == UNSPEC or h <= 23) and (mi == UNSPEC or mi <= 59) and (s == UNSPEC or s <= 59) and (hu == UNSPEC or hu <= 99)


def model_enc(cmd, args):
    if cmd == "enc_null":
        return b"\x00"
    if cmd == "enc_bool":
        return bytes([0x10 | args[0]])
    if cmd == "enc_unsigned":
        return prim(2, uint_content(args[0]))
    if cmd == "enc_enum":
        return prim(9, uint_content(args[0]))
    if cmd == "enc_signed":
        return prim(3, sint_content(args[0]))
    if cmd == "enc_real":
        return prim(4, args[0].to_bytes(4, "big"))
    if cmd == "enc_octets":
        return prim(6, args[0])
    if cmd == "enc_str":
        return prim(7, b"\x00" + args[0])
    if cmd == "enc_bits":
        bits = args[0]
        nb = (len(bits) + 7) // 8
        data = bytearray(nb)
        for i, b in enumerate(bits):
            if b:
                data[i // 8] |= 0x80 >> (i % 8)
        return prim(8, bytes([(-len(bits)) % 8]) + bytes(data))
    if cmd == "enc_date":
        y, m, d, w = args
        if y == 0xFFFF:
            yb = 255
        elif 1900 <= y <= 2154:
            yb = y - 1900
        else:
            return None
        if not date_ok(m, d, w):
            return None
        return prim(10, bytes([yb, m, d, w]))
    if cmd == "enc_time":
        if not time_ok(*args):
            return None
        return prim(11, bytes(args))
    if cmd == "enc_oid":
        t, inst = args
        if t > 1023 or inst > 0x3FFFFF:
            return None
        return prim(12, ((t << 22) | inst).to_bytes(4, "big"))
    if cmd == "enc_ctx_unsigned":
        return prim(args[0], uint_content(args[1]), ctx=True)
    if cmd == "enc_ctx_bool":
        return prim(args[0], bytes([args[1]]), ctx=True)
    if cmd in ("enc_open", "enc_close"):
        tag = args[0]
        if tag == 255:
            return None
        lvt = 6 if cmd == "enc_open" else 7
        return bytes([0xF8 | lvt, tag]) if tag >= 15 else bytes([(tag << 4) | 0x08 | lvt])
    raise KeyError(cmd)


def fmt_enc(res, cap):
    if res is None or len(res) > cap:
        return "ERR"
    return "OK " + fmt_bytes(res)


# ---------------------------------------------------------------- decoder model
def model_dec_tag(b):
    """Returns (hl, tag, ctx, kind, lvt) or None.  kind in {'open','close',None}."""
    if not b:
        return None
    tag, ctx, lvt = b[0] >> 4, bool(b[0] & 8), b[0] & 7
    i = 1
    if tag == 15:
        if len(b) < 2 or b[1] == 255:
            return None
        tag, i = b[1], 2
    if lvt in (6, 7):
        if not ctx:
            return None
        return (i, tag, ctx, "open" if lvt == 6 else "close", 0)
    if lvt == 5:
        if len(b) < i + 1:
            return None
        e = b[i]
        i += 1
        if e <= 253:
            lvt = e
        else:
            n = 2 if e == 254 else 4
            if len(b) < i + n:
                return None
            lvt = int.from_bytes(b[i:i + n], "big")
            i += n
    return (i, tag, ctx, None, lvt)


def fmt_dec_tag(b):
    t = model_dec_tag(b)
    if t is None:
        return "ERR"
    hl, tag, ctx, kind, lvt = t
    s = f"OK hl={hl} tag={tag} {'ctx' if ctx else 'app'}"
    return s + (f" {kind}" if kind else f" lvt={lvt}")


def fmt_dec_app(b):
    t = model_dec_tag(b)
    if t is None:
        return "ERR"
    hl, tag, ctx, kind, lvt = t
    if ctx:
        return "ERR"
    if tag == 1:
        raw = b[0] & 7
        return f"OK n={hl} bool {raw}" if raw <= 1 else "ERR"
    if len(b) - hl < lvt:
        return "ERR"
    c = b[hl:hl + lvt]
    n = hl + lvt
    if tag == 0:
        return f"OK n={n} null" if lvt == 0 else "ERR"
    if tag in (2, 9):
        if not 1 <= lvt <= 4:
            return "ERR"
        return f"OK n={n} {'u' if tag == 2 else 'enum'} {int.from_bytes(c, 'big')}"
    if tag == 3:
        if not 1 <= lvt <= 4:
            return "ERR"
        return f"OK n={n} i {int.from_bytes(c, 'big', signed=True)}"
    if tag == 4:
        return f"OK n={n} r {c.hex()}" if lvt == 4 else "ERR"
    if tag == 6:
        return f"OK n={n} oct {fmt_bytes(c)}"
    if tag == 7:
        if lvt < 1:
            return "ERR"
        return f"OK n={n} str cs={c[0]} {fmt_bytes(c[1:])}"
    if tag == 8:
        if lvt < 1 or c[0] > 7 or (lvt == 1 and c[0] != 0):
            return "ERR"
        return f"OK n={n} bits {fmt_bits(c[1:], (lvt - 1) * 8 - c[0])}"
    if tag == 10:
        if lvt != 4 or not date_ok(c[1], c[2], c[3]):
            return "ERR"
        y = 65535 if c[0] == 255 else 1900 + c[0]
        return f"OK n={n} date {y} {c[1]} {c[2]} {c[3]}"
    if tag == 11:
        if lvt != 4 or not time_ok(*c):
            return "ERR"
        return f"OK n={n} time {c[0]} {c[1]} {c[2]} {c[3]}"
    if tag == 12:
        if lvt != 4:
            return "ERR"
        v = int.from_bytes(c, "big")
        return f"OK n={n} oid {v >> 22} {v & 0x3FFFFF}"
    return "ERR"  # Double (no union member) and reserved tags


# ---------------------------------------------------------------- generation
def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 20240
    rnd = random.Random(seed)
    blocks = []

    def block(name, lines):
        blocks.append((name, lines))

    def interesting_u32():
        return rnd.choice([0, 1, 4, 5, 0x7F, 0x80, 0xFF, 0x100, 0xFFFF, 0x10000, 0xFFFFFF, 0x1000000,
                           0xFFFFFFFF, rnd.getrandbits(8), rnd.getrandbits(16), rnd.getrandbits(24), rnd.getrandbits(32)])

    def interesting_s32():
        v = rnd.choice([0, -1, 127, 128, -128, -129, 32767, 32768, -32768, -32769, 8388607, 8388608,
                        -8388608, -8388609, 2**31 - 1, -2**31, rnd.getrandbits(8), rnd.getrandbits(16),
                        rnd.getrandbits(24), rnd.getrandbits(32)])
        return v - (1 << 32) if v >= (1 << 31) else v

    def rand_bytes(n):
        return bytes(rnd.getrandbits(8) for _ in range(n))

    # (cmd, argv string, model args)
    def gen_enc_case():
        k = rnd.randrange(15)
        if k == 0:
            return "enc_null", "", []
        if k == 1:
            v = rnd.randrange(2)
            return "enc_bool", f"{v}", [v]
        if k == 2:
            v = interesting_u32()
            return "enc_unsigned", f"{v}", [v]
        if k == 3:
            v = interesting_u32()
            return "enc_enum", f"{v}", [v]
        if k == 4:
            v = interesting_s32()
            return "enc_signed", f"{v}", [v]
        if k == 5:
            v = rnd.getrandbits(32)
            return "enc_real", f"{v:08x}", [v]
        if k == 6:
            b = rand_bytes(rnd.choice([0, 1, 4, 5, 60, 252, 253, 254, 255, 300]))
            return "enc_octets", arg_bytes(b), [b]
        if k == 7:
            b = rand_bytes(rnd.choice([0, 3, 4, 5, 252, 253, 254]))
            return "enc_str", arg_bytes(b), [b]
        if k == 8:
            bits = [rnd.randrange(2) for _ in range(rnd.choice([0, 1, 7, 8, 9, 16, 17, 255, 256, 257, 2016, 2023, 2024, 2025]))]
            return "enc_bits", arg_bits(bits), [bits]
        if k == 9:
            y = rnd.choice([1899, 1900, 2024, 2154, 2155, 0, 65535, rnd.randrange(65536)])
            m = rnd.choice([0, 1, 12, 13, 14, 15, 255, rnd.randrange(256)])
            d = rnd.choice([0, 1, 31, 32, 33, 34, 35, 255, rnd.randrange(256)])
            w = rnd.choice([0, 1, 7, 8, 255, rnd.randrange(256)])
            return "enc_date", f"{y} {m} {d} {w}", [y, m, d, w]
        if k == 10:
            a = [rnd.choice([0, 23, 24, 255, rnd.randrange(256)]), rnd.choice([0, 59, 60, 255, rnd.randrange(256)]),
                 rnd.choice([0, 59, 60, 255, rnd.randrange(256)]), rnd.choice([0, 99, 100, 255, rnd.randrange(256)])]
            return "enc_time", " ".join(map(str, a)), a
        if k == 11:
            t = rnd.choice([0, 8, 1023, 1024, 65535, rnd.randrange(1024)])
            i = rnd.choice([0, 0x3FFFFF, 0x400000, 0xFFFFFFFF, rnd.randrange(0x400000)])
            return "enc_oid", f"{t} {i}", [t, i]
        tag = rnd.choice([0, 1, 14, 15, 16, 200, 254, 255, rnd.randrange(256)])
        if k == 12:
            v = interesting_u32()
            return "enc_ctx_unsigned", f"{tag} {v}", [tag, v]
        if k == 13:
            v = rnd.randrange(2)
            return "enc_ctx_bool", f"{tag} {v}", [tag, v]
        c = rnd.choice(["enc_open", "enc_close"])
        return c, f"{tag}", [tag]

    # 1. random encoder cases, each at default capacity and at exact / exact-1 capacity
    for bi in range(60):
        inp, exp = [], []
        for _ in range(25):
            cmd, argv, margs = gen_enc_case()
            res = model_enc(cmd, margs)
            caps = [1024]
            if res is not None:
                caps += [len(res), len(res) - 1, 0]
            for cap in caps:
                if cap < 0:
                    continue
                line = f"{cmd} {argv}".strip()
                if cap != 1024:
                    line += f" @{cap}"
                elif res is not None and len(res) > 1024:
                    continue
                inp.append(line)
                exp.append(fmt_enc(res, cap))
        block(f"gen-enc-{bi}", (inp, exp))

    # 2. large values that need the 2- and 4-octet extended length forms
    inp, exp = [], []
    for n in [253, 254, 255, 1000, 65531, 65532, 65533, 65534, 65535, 65536, 65537, 70000, 79990]:
        b = bytes((i * 7 + n) & 0xFF for i in range(n))
        res = model_enc("enc_octets", [b])
        for cap in [len(res), len(res) - 1]:
            if cap > 80000:
                continue
            inp.append(f"enc_octets {arg_bytes(b)} @{cap}")
            exp.append(fmt_enc(res, cap))
        res = model_enc("enc_str", [b])
        if len(res) <= 80000:
            inp.append(f"enc_str rep:41:{n} @{len(res)}")
            exp.append(fmt_enc(model_enc("enc_str", [b"A" * n]), len(res)))
    for nbits in [8 * 252, 8 * 252 + 1, 8 * 253, 8 * 9999 + 5, 79999, 80000]:
        bits = [1] * nbits
        res = model_enc("enc_bits", [bits])
        for cap in [len(res), len(res) - 1]:
            if cap > 80000:
                continue
            inp.append(f"enc_bits rep:1:{nbits} @{cap}")
            exp.append(fmt_enc(res, cap))
    block("gen-large", (inp, exp))

    # 3. round trips: decode what the model encodes (application-tagged only)
    app_cmds = {"enc_null", "enc_bool", "enc_unsigned", "enc_enum", "enc_signed", "enc_real", "enc_octets",
                "enc_str", "enc_bits", "enc_date", "enc_time", "enc_oid"}
    for bi in range(40):
        inp, exp = [], []
        while len(inp) < 25:
            cmd, argv, margs = gen_enc_case()
            if cmd not in app_cmds:
                continue
            res = model_enc(cmd, margs)
            if res is None:
                continue
            inp.append(f"dec_app {arg_bytes(res)}")
            exp.append(fmt_dec_app(res))
            inp.append(f"dec_tag {arg_bytes(res)}")
            exp.append(fmt_dec_tag(res))
            # truncation: every strict prefix of a short encoding must be rejected
            if len(res) <= 12:
                for k in range(len(res)):
                    inp.append(f"dec_app {arg_bytes(res[:k])}")
                    exp.append(fmt_dec_app(res[:k]))
            # trailing garbage must not be consumed
            tail = res + bytes([0x21, 0x48])
            inp.append(f"dec_app {arg_bytes(tail)}")
            exp.append(fmt_dec_app(tail))
        block(f"gen-roundtrip-{bi}", (inp, exp))

    # 4. fuzz: random short byte strings through both decoders
    for bi in range(60):
        inp, exp = [], []
        for _ in range(40):
            n = rnd.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12])
            b = rand_bytes(n)
            if n and rnd.random() < 0.5:  # bias towards application tags with plausible lengths
                tag = rnd.randrange(16)
                lvt = rnd.randrange(8)
                b = bytes([(tag << 4) | (rnd.random() < 0.15) * 8 | lvt]) + b[1:]
            inp.append(f"dec_app {arg_bytes(b)}")
            exp.append(fmt_dec_app(b))
            inp.append(f"dec_tag {arg_bytes(b)}")
            exp.append(fmt_dec_tag(b))
        block(f"gen-fuzz-{bi}", (inp, exp))

    # 5. extended-length decode forms
    inp, exp = [], []
    for b in [bytes([0x65, 0x05]) + b"\x11" * 5,
              bytes([0x65, 0xFE, 0x01, 0x00]) + b"\x22" * 256,
              bytes([0x65, 0xFF, 0x00, 0x01, 0x00, 0x00]) + b"\x33" * 65536,
              bytes([0x65, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]) + b"\x33" * 16,
              bytes([0x65, 0xFF, 0x00, 0x01, 0x00, 0x01]) + b"\x33" * 65536,
              bytes([0x75, 0xFE, 0x01, 0x00, 0x00]) + b"\x41" * 255,
              bytes([0x85, 0xFE, 0x01, 0x00, 0x03]) + b"\xff" * 255,
              bytes([0x85, 0xFF, 0x00, 0x01, 0x00, 0x00, 0x05]) + b"\x5a" * 65535]:
        inp.append(f"dec_app {arg_bytes(b)}")
        exp.append(fmt_dec_app(b))
        inp.append(f"dec_tag {arg_bytes(b)}")
        exp.append(fmt_dec_tag(b))
    block("gen-dec-extended", (inp, exp))

    print(f"# Generated by tests/gen_vectors.py seed={seed}; do not edit by hand.")
    for name, (inp, exp) in blocks:
        print(f"### {name}")
        print("\n".join(inp))
        print("---")
        print("\n".join(exp))


if __name__ == "__main__":
    main()

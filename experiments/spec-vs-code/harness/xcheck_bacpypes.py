#!/usr/bin/env python3
"""Cross-check standard-defined P oracle vectors against bacpypes3 (independent implementation)."""
import struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_vectors import parse_vec
from bacpypes3.primitivedata import (Null, Boolean, Unsigned, Integer, Real, Double, OctetString,
    CharacterString, BitString, Enumerated, Date, Time, ObjectIdentifier, OpeningTag, ClosingTag, Tag, TagList, TagClass)
from bacpypes3.pdu import PDUData

def fnv(b):
    h = 1469598103934665603
    for x in b:
        h ^= x; h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h

def fmt(b):
    if len(b) == 0: return "-"
    if len(b) <= 64: return b.hex()
    return b[:16].hex() + ".." + format(fnv(b), "016x") + "/" + str(len(b))

def parse_bytes(s):
    if s == "-": return b""
    out = b""
    for seg in s.split("+"):
        if seg.startswith("rep:"):
            _, hh, n = seg.split(":"); out += bytes([int(hh, 16)]) * int(n)
        else:
            out += bytes.fromhex(seg)
    return out

def parse_bits(s):
    if s == "-": return []
    out = []
    for seg in s.split("+"):
        if seg.startswith("rep:"):
            _, d, n = seg.split(":"); out += [int(d)] * int(n)
        else:
            out += [int(c) for c in seg]
    return out

def tagbytes(tag):
    return bytes(tag.encode().pduData)

def app(obj):
    tl = obj.encode()
    return b"".join(tagbytes(t) for t in tl.tagList)

def ctx(obj, tagnum):
    t = obj.encode().tagList[0]
    return tagbytes(t.app_to_context(tagnum))

def bp_encode(cmd):
    a = cmd.split()
    c = a[0]
    if c == "enc_null": return app(Null(()))
    if c == "enc_bool": return app(Boolean(bool(int(a[1]))))
    if c == "enc_unsigned": return app(Unsigned(int(a[1])))
    if c == "enc_enum": return app(Enumerated(int(a[1])))
    if c == "enc_signed": return app(Integer(int(a[1])))
    if c == "enc_real": return app(Real(struct.unpack(">f", bytes.fromhex(a[1]))[0]))
    if c == "enc_double": return app(Double(struct.unpack(">d", bytes.fromhex(a[1]))[0]))
    if c == "enc_octets": return app(OctetString(parse_bytes(a[1])))
    if c == "enc_str": return app(CharacterString(parse_bytes(a[1]).decode("utf-8")))
    if c == "enc_bits": return app(BitString(parse_bits(a[1])))
    if c == "enc_date":
        y = int(a[1]); y = 255 if y == 65535 else y - 1900
        return app(Date((y, int(a[2]), int(a[3]), int(a[4]))))
    if c == "enc_time": return app(Time(tuple(int(x) for x in a[1:5])))
    if c == "enc_oid": return app(ObjectIdentifier((int(a[1]), int(a[2]))))
    if c == "enc_ctx_unsigned": return ctx(Unsigned(int(a[2])), int(a[1]))
    if c == "enc_ctx_enum": return ctx(Enumerated(int(a[2])), int(a[1]))
    if c == "enc_ctx_signed": return ctx(Integer(int(a[2])), int(a[1]))
    if c == "enc_ctx_bool": return ctx(Boolean(bool(int(a[2]))), int(a[1]))
    if c == "enc_open": return tagbytes(OpeningTag(int(a[1])))
    if c == "enc_close": return tagbytes(ClosingTag(int(a[1])))
    raise KeyError(c)

CLS = {0: Null, 1: Boolean, 2: Unsigned, 3: Integer, 4: Real, 5: Double, 6: OctetString, 7: CharacterString,
       8: BitString, 9: Enumerated, 10: Date, 11: Time, 12: ObjectIdentifier}

def bp_decode(hexarg):
    data = parse_bytes(hexarg)
    pd = PDUData(data)
    t = Tag.decode(pd)
    consumed = len(data) - len(pd.pduData)
    if t.tag_class != TagClass.application:
        return "ERR"
    v = CLS[t.tag_number].decode(TagList([t]))
    n = t.tag_number
    if n == 0: s = "null"
    elif n == 1: s = f"bool {1 if v else 0}"
    elif n == 2: s = f"u {int(v)}"
    elif n == 3: s = f"i {int(v)}"
    elif n == 4: s = "r " + struct.pack(">f", float(v)).hex()
    elif n == 5: s = "d " + struct.pack(">d", float(v)).hex()
    elif n == 6: s = "oct " + fmt(bytes(v))
    elif n == 7: s = f"str cs=0 " + fmt(str(v).encode("utf-8"))
    elif n == 8:
        bits = list(v); s = "bits " + ("-" if not bits else "".join(str(b) for b in bits))
    elif n == 9: s = f"enum {int(v)}"
    elif n == 10:
        y, m, d, w = v; s = f"date {65535 if y == 255 else 1900 + y} {m} {d} {w}"
    elif n == 11: s = "time " + " ".join(str(x) for x in v)
    elif n == 12:
        ty, inst = v; s = f"oid {int(ty) if not isinstance(ty, str) else ty} {inst}"
    return f"OK n={consumed} {s}"

STD = {"G2", "G3", "G4", "G5", "E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9", "E10", "E11", "E12",
       "E13", "E14", "DV", "D6", "CR1", "D1"}
def main():
    vec = sys.argv[1]
    agree = disagree = skipped = 0
    for b in parse_vec(vec):
        tags = set(b["tags"])
        if not (tags & STD) or "G1" in tags or len(b["inp"]) != 1:
            continue
        cmd, exp = b["inp"][0], b["exp"][0]
        if "@" in cmd or cmd.startswith("dec_tag"):
            skipped += 1; continue
        try:
            if cmd.startswith("dec_app"):
                if not exp.startswith("OK"):
                    skipped += 1; continue
                got = bp_decode(cmd.split()[1])
            else:
                if not exp.startswith("OK"):
                    skipped += 1; continue
                got = "OK " + fmt(bp_encode(cmd))
        except Exception as e:
            got = f"EXC {type(e).__name__}: {e}"
        if got == exp:
            agree += 1
        else:
            disagree += 1
            print(f"DIFF {b['id']} {b['tags']} {cmd[:60]}\n   ref : {exp[:100]}\n   bp3 : {got[:100]}")
    print(f"agree={agree} disagree={disagree} skipped={skipped}")

main()

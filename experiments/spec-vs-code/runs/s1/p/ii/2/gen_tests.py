#!/usr/bin/env python3
"""Generates tests.vec. Expected encodings are written out by hand from
ASHRAE 135 clause 20.2; this script only builds long byte strings and applies
the driver's print format (hash/length abbreviation) to them."""

def fnv1a(b):
    h = 1469598103934665603
    for x in b:
        h ^= x
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h

def pb(b):
    """driver print_bytes"""
    if len(b) == 0:
        return "-"
    if len(b) <= 64:
        return b.hex()
    return b[:16].hex() + "..%016x/%d" % (fnv1a(b), len(b))

def pbits(packed, nbits):
    """driver print_bitdigits"""
    if nbits == 0:
        return "-"
    if nbits <= 256:
        return "".join("1" if (packed[i // 8] >> (7 - i % 8)) & 1 else "0" for i in range(nbits))
    nb = (nbits + 7) // 8
    return "%dbits..%016x" % (nbits, fnv1a(packed[:nb - 1]) ^ (packed[nb - 1] >> (nb * 8 - nbits)))

H = bytes.fromhex
out = ["# Unit tests for bacapp.c (vector format: see run_vectors.py / driver.c).",
       "# Expected encodings follow ASHRAE 135 clause 20.2.", ""]

def block(name, pairs, tags=""):
    out.append("### " + name + (" " + tags if tags else ""))
    for i, _ in pairs:
        out.append(i)
    out.append("---")
    for _, e in pairs:
        out.append(e)

def enc(cmd, hexout):
    return (cmd, "OK " + hexout if hexout != "ERR" else "ERR")

# ---------------------------------------------------------------- headers
block("tag-ctx-unsigned-short-tags", [
    enc("enc_ctx_unsigned 0 0", "0900"),
    enc("enc_ctx_unsigned 1 255", "19ff"),
    enc("enc_ctx_unsigned 14 5", "e905"),
    enc("enc_ctx_unsigned 3 4294967295", "3cffffffff"),
    enc("enc_ctx_unsigned 2 65536", "2b010000"),
])
block("tag-ctx-unsigned-extended-tags", [
    enc("enc_ctx_unsigned 15 5", "f90f05"),
    enc("enc_ctx_unsigned 100 256", "fa640100"),
    enc("enc_ctx_unsigned 254 0", "f9fe00"),
    enc("enc_ctx_unsigned 255 0", "ERR"),
])
block("tag-ctx-boolean", [
    enc("enc_ctx_bool 0 1", "0901"),
    enc("enc_ctx_bool 2 0", "2900"),
    enc("enc_ctx_bool 20 1", "f91401"),
    enc("enc_ctx_bool 255 1", "ERR"),
])
block("tag-opening-closing", [
    enc("enc_open 0", "0e"),
    enc("enc_close 0", "0f"),
    enc("enc_open 14", "ee"),
    enc("enc_close 14", "ef"),
    enc("enc_open 15", "fe0f"),
    enc("enc_close 15", "ff0f"),
    enc("enc_open 254", "fefe"),
    enc("enc_close 254", "fffe"),
    enc("enc_open 255", "ERR"),
    enc("enc_close 255", "ERR"),
])
block("dec-tag-opening-closing", [
    ("dec_tag 0e", "OK hl=1 tag=0 ctx open"),
    ("dec_tag 1f", "OK hl=1 tag=1 ctx close"),
    ("dec_tag fe0f", "OK hl=2 tag=15 ctx open"),
    ("dec_tag fffe", "OK hl=2 tag=254 ctx close"),
])
block("dec-tag-app-class-lengths", [
    ("dec_tag 00", "OK hl=1 tag=0 app lvt=0"),
    ("dec_tag 11", "OK hl=1 tag=1 app lvt=1"),
    ("dec_tag 24", "OK hl=1 tag=2 app lvt=4"),
    ("dec_tag 6505", "OK hl=2 tag=6 app lvt=5"),
    ("dec_tag 65fd", "OK hl=2 tag=6 app lvt=253"),
    ("dec_tag 65fe00fe", "OK hl=4 tag=6 app lvt=254"),
    ("dec_tag 65feffff", "OK hl=4 tag=6 app lvt=65535"),
    ("dec_tag 65ff00010000", "OK hl=6 tag=6 app lvt=65536"),
    ("dec_tag 65ffffffffff", "OK hl=6 tag=6 app lvt=4294967295"),
])
block("dec-tag-extended-tag-and-length", [
    ("dec_tag f90f", "OK hl=2 tag=15 ctx lvt=1"),
    ("dec_tag fd0f05", "OK hl=3 tag=15 ctx lvt=5"),
    ("dec_tag fdfefeffff", "OK hl=5 tag=254 ctx lvt=65535"),
    ("dec_tag fdfeff00010000", "OK hl=7 tag=254 ctx lvt=65536"),
])
block("dec-tag-header-only", [
    # only the header is decoded; content presence is bac_dec_app's job
    ("dec_tag 29", "OK hl=1 tag=2 ctx lvt=1"),
    ("dec_tag 2148ffff", "OK hl=1 tag=2 app lvt=1"),
])
block("dec-tag-malformed", [
    ("dec_tag -", "ERR"),
    ("dec_tag f9", "ERR"),            # extended tag number octet missing
    ("dec_tag f9ff05", "ERR"),        # tag number X'FF' is reserved
    ("dec_tag 06", "ERR"),            # application class cannot open
    ("dec_tag 07", "ERR"),            # application class cannot close
    ("dec_tag 65", "ERR"),            # extended length octet missing
    ("dec_tag 65fe00", "ERR"),        # 2-octet length truncated
    ("dec_tag 65ff000100", "ERR"),    # 4-octet length truncated
    ("dec_tag fd0f", "ERR"),          # length after extended tag missing
])

# ---------------------------------------------------------------- null / boolean
block("null", [
    enc("enc_null", "00"),
    enc("enc_null @1", "00"),
    enc("enc_null @0", "ERR"),
    ("dec_app 00", "OK n=1 null"),
    ("dec_app 0100", "ERR"),          # null has no content
])
block("boolean-app", [
    enc("enc_bool 0", "10"),
    enc("enc_bool 1", "11"),
    enc("enc_bool 1 @0", "ERR"),
    ("dec_app 10", "OK n=1 bool 0"),
    ("dec_app 11", "OK n=1 bool 1"),
    ("dec_app 12", "ERR"),            # LVT value must be 0 or 1
    ("dec_app 1501", "ERR"),          # LVT=5 is not a boolean value
])

# ---------------------------------------------------------------- unsigned / enumerated
block("unsigned-octet-boundaries", [
    enc("enc_unsigned 0", "2100"),
    enc("enc_unsigned 255", "21ff"),
    enc("enc_unsigned 256", "220100"),
    enc("enc_unsigned 65535", "22ffff"),
    enc("enc_unsigned 65536", "23010000"),
    enc("enc_unsigned 16777215", "23ffffff"),
    enc("enc_unsigned 16777216", "2401000000"),
    enc("enc_unsigned 4294967295", "24ffffffff"),
])
block("unsigned-decode", [
    ("dec_app 2100", "OK n=2 u 0"),
    ("dec_app 220100", "OK n=3 u 256"),
    ("dec_app 23010000", "OK n=4 u 65536"),
    ("dec_app 24ffffffff", "OK n=5 u 4294967295"),
    ("dec_app 220048", "OK n=3 u 72"),          # redundant leading zero accepted
    ("dec_app 25050000000048", "OK n=7 u 72"),  # still fits 32 bits
    ("dec_app 25050100000000", "ERR"),          # 2^32 does not fit
    ("dec_app 20", "ERR"),                      # at least one content octet
    ("dec_app 2201", "ERR"),                    # truncated
    ("dec_app 2148ff", "OK n=2 u 72"),          # trailing data not consumed
])
block("enumerated", [
    enc("enc_enum 255", "91ff"),
    enc("enc_enum 256", "920100"),
    enc("enc_enum 4294967295", "94ffffffff"),
    ("dec_app 9100", "OK n=2 enum 0"),
    ("dec_app 94ffffffff", "OK n=5 enum 4294967295"),
    ("dec_app 90", "ERR"),
])

# ---------------------------------------------------------------- signed
block("signed-octet-boundaries", [
    enc("enc_signed 0", "3100"),
    enc("enc_signed -1", "31ff"),
    enc("enc_signed 127", "317f"),
    enc("enc_signed 128", "320080"),
    enc("enc_signed -128", "3180"),
    enc("enc_signed -129", "32ff7f"),
    enc("enc_signed 32767", "327fff"),
    enc("enc_signed 32768", "33008000"),
    enc("enc_signed -32768", "328000"),
    enc("enc_signed -32769", "33ff7fff"),
    enc("enc_signed 8388607", "337fffff"),
    enc("enc_signed 8388608", "3400800000"),
    enc("enc_signed -8388608", "33800000"),
    enc("enc_signed -8388609", "34ff7fffff"),
    enc("enc_signed 2147483647", "347fffffff"),
    enc("enc_signed -2147483648", "3480000000"),
])
block("signed-decode", [
    ("dec_app 3100", "OK n=2 i 0"),
    ("dec_app 31ff", "OK n=2 i -1"),
    ("dec_app 3180", "OK n=2 i -128"),
    ("dec_app 32ff7f", "OK n=3 i -129"),
    ("dec_app 320080", "OK n=3 i 128"),
    ("dec_app 33800000", "OK n=4 i -8388608"),
    ("dec_app 347fffffff", "OK n=5 i 2147483647"),
    ("dec_app 3480000000", "OK n=5 i -2147483648"),
    ("dec_app 32ffff", "OK n=3 i -1"),            # redundant sign octet accepted
    ("dec_app 3505ffffffff80", "OK n=7 i -128"),  # redundant sign octets accepted
    ("dec_app 3505ff7fffffff", "ERR"),            # -2^31-1 out of range
    ("dec_app 35050080000000", "ERR"),            # 2^31 out of range
    ("dec_app 30", "ERR"),
    ("dec_app 33ffff", "ERR"),
])

# ---------------------------------------------------------------- real / double
block("real", [
    enc("enc_real 00000000", "4400000000"),
    enc("enc_real 80000000", "4480000000"),
    enc("enc_real 3f800000", "443f800000"),
    enc("enc_real 7f800000", "447f800000"),
    enc("enc_real 7fc00001", "447fc00001"),
    enc("enc_real 00000001", "4400000001"),
    enc("enc_real 3f800000 @4", "ERR"),
    enc("enc_real 3f800000 @5", "443f800000"),
    ("dec_app 443f800000", "OK n=5 r 3f800000"),
    ("dec_app 4480000000", "OK n=5 r 80000000"),
    ("dec_app 447fc00001", "OK n=5 r 7fc00001"),
    ("dec_app 43000000", "ERR"),                  # REAL must be 4 octets
    ("dec_app 45050000000000", "ERR"),
    ("dec_app 443f80", "ERR"),                    # truncated
])
block("double-unsupported", [
    ("dec_app 55083ff0000000000000", "ERR"),      # no double member in bac_value_t
])

# ---------------------------------------------------------------- octet string
os253 = bytes([0xab]) * 253
os254 = bytes([0xab]) * 254
os65535 = bytes([0xcd]) * 65535
os65536 = bytes([0xcd]) * 65536
block("octet-string-lengths", [
    enc("enc_octets -", "60"),
    enc("enc_octets 01020304", "6401020304"),
    enc("enc_octets 0102030405", "65050102030405"),
    enc("enc_octets rep:ab:253", pb(H("65fd") + os253)),
    enc("enc_octets rep:ab:254", pb(H("65fe00fe") + os254)),
    enc("enc_octets rep:cd:65535 @70000", pb(H("65feffff") + os65535)),
    enc("enc_octets rep:cd:65536 @70000", pb(H("65ff00010000") + os65536)),
])
block("octet-string-capacity", [
    enc("enc_octets 01020304 @4", "ERR"),
    enc("enc_octets 01020304 @5", "6401020304"),
    enc("enc_octets rep:ab:253 @254", "ERR"),
    enc("enc_octets rep:ab:253 @255", pb(H("65fd") + os253)),
    enc("enc_octets rep:ab:254 @257", "ERR"),
    enc("enc_octets rep:ab:79994 @80000", pb(H("65ff") + (79994).to_bytes(4, "big") + bytes([0xab]) * 79994)),
    enc("enc_octets rep:ab:79995 @80000", "ERR"),
    enc("enc_octets - @0", "ERR"),
    enc("enc_octets - @1", "60"),
    enc("enc_octets rep:ab:5 @1023", "6505ababababab"),
    enc("enc_octets rep:ab:2000", "ERR"),              # default capacity is 1024
])
block("octet-string-decode", [
    ("dec_app 60", "OK n=1 oct -"),
    ("dec_app 631234ff", "OK n=4 oct 1234ff"),
    ("dec_app 65fd+rep:ab:253", "OK n=255 oct " + pb(os253)),
    ("dec_app 65feffff+rep:cd:65535", "OK n=65539 oct " + pb(os65535)),
    ("dec_app 65ff00010000+rep:cd:65536", "OK n=65542 oct " + pb(os65536)),
    ("dec_app 631234", "ERR"),
    ("dec_app 65ff00010000+rep:cd:65535", "ERR"),
    ("dec_app 65ffffffffff+00", "ERR"),
])

# ---------------------------------------------------------------- character string
s65535 = b"A" * 65535
block("char-string", [
    enc("enc_str -", "7100"),
    enc("enc_str 41", "720041"),
    enc("enc_str 41 @2", "ERR"),
    enc("enc_str 41 @3", "720041"),
    enc("enc_str 410042", "7400410042"),          # embedded NUL is data
    enc("enc_str 41424344", "750500" + "41424344"),
    enc("enc_str c3a4e282ac", "750600c3a4e282ac"),
    enc("enc_str rep:41:65535 @70000", pb(H("75ff0001000000") + s65535)),
    ("dec_app 7100", "OK n=2 str cs=0 -"),
    ("dec_app 720041", "OK n=3 str cs=0 41"),
    ("dec_app 720341", "OK n=3 str cs=3 41"),     # character set passed through
    ("dec_app 75ff0001000000+rep:41:65535", "OK n=65542 str cs=0 " + pb(s65535)),
    ("dec_app 70", "ERR"),                         # character-set octet missing
    ("dec_app 7300", "ERR"),                       # truncated
])

# ---------------------------------------------------------------- bit string
big = bytes([0xff]) * 254
bits2031 = bytes([0xff]) * 253 + bytes([0xfe])
block("bit-string-encode", [
    enc("enc_bits -", "8100"),
    enc("enc_bits 1", "820780"),
    enc("enc_bits 00000000", "820000"),
    enc("enc_bits 11111111", "8200ff"),
    enc("enc_bits 111111111", "8307ff80"),
    enc("enc_bits 0000000000000001", "83000001"),
    enc("enc_bits 12", "8206c0"),                  # non-zero means 1, no bleed
    enc("enc_bits 10101 @2", "ERR"),
    enc("enc_bits 10101 @3", "8203a8"),
    enc("enc_bits rep:1:2032", pb(H("85fe00ff") + bytes([0x00]) + big)),
    enc("enc_bits rep:1:2031", pb(H("85fe00ff") + bytes([0x01]) + bits2031)),
])
block("bit-string-decode", [
    ("dec_app 8100", "OK n=2 bits -"),
    ("dec_app 8203a8", "OK n=3 bits 10101"),
    ("dec_app 8203af", "OK n=3 bits 10101"),       # pad bits ignored
    ("dec_app 8307ff80", "OK n=4 bits 111111111"),
    ("dec_app 85fe00ff+00+rep:ff:254", "OK n=259 bits " + pbits(big, 2032)),
    ("dec_app 85fe00ff+01+rep:ff:253+fe", "OK n=259 bits " + pbits(bits2031, 2031)),
    ("dec_app 80", "ERR"),                         # unused-bits octet missing
    ("dec_app 8101", "ERR"),                       # unused bits without data
    ("dec_app 8208ff", "ERR"),                     # unused bits > 7
    ("dec_app 8203", "ERR"),                       # truncated
])

# ---------------------------------------------------------------- date / time
block("date-encode", [
    enc("enc_date 1900 1 1 1", "a400010101"),
    enc("enc_date 2154 12 31 7", "a4fe0c1f07"),
    enc("enc_date 2024 13 32 255", "a47c0d20ff"),
    enc("enc_date 2024 14 33 1", "a47c0e2101"),
    enc("enc_date 2024 2 34 1", "a47c022201"),
    enc("enc_date 65535 255 255 255", "a4ffffffff"),
    enc("enc_date 1899 1 1 1", "ERR"),
    enc("enc_date 2155 1 1 1", "ERR"),
    enc("enc_date 255 1 1 1", "ERR"),              # calendar year, not offset
    enc("enc_date 2024 0 1 1", "ERR"),
    enc("enc_date 2024 15 1 1", "ERR"),
    enc("enc_date 2024 1 0 1", "ERR"),
    enc("enc_date 2024 1 35 1", "ERR"),
    enc("enc_date 2024 1 1 0", "ERR"),
    enc("enc_date 2024 1 1 8", "ERR"),
    enc("enc_date 2024 1 1 1 @4", "ERR"),
])
block("date-decode", [
    ("dec_app a45b011804", "OK n=5 date 1991 1 24 4"),
    ("dec_app a400010101", "OK n=5 date 1900 1 1 1"),
    ("dec_app a4fe0c1f07", "OK n=5 date 2154 12 31 7"),
    ("dec_app a4ff0102ff", "OK n=5 date 65535 1 2 255"),
    ("dec_app a47c000000", "OK n=5 date 2024 0 0 0"),  # fields passed through
    ("dec_app a37c0d20", "ERR"),
    ("dec_app a47c0d20", "ERR"),
])
block("time", [
    enc("enc_time 0 0 0 0", "b400000000"),
    enc("enc_time 23 59 59 99", "b4173b3b63"),
    enc("enc_time 255 255 255 255", "b4ffffffff"),
    enc("enc_time 12 255 0 255", "b40cff00ff"),
    enc("enc_time 24 0 0 0", "ERR"),
    enc("enc_time 0 60 0 0", "ERR"),
    enc("enc_time 0 0 60 0", "ERR"),
    enc("enc_time 0 0 0 100", "ERR"),
    ("dec_app b411232d11", "OK n=5 time 17 35 45 17"),
    ("dec_app b4ffffffff", "OK n=5 time 255 255 255 255"),
    ("dec_app b5050000000000", "ERR"),
    ("dec_app b4000000", "ERR"),
])

# ---------------------------------------------------------------- object identifier
block("object-id", [
    enc("enc_oid 0 0", "c400000000"),
    enc("enc_oid 8 4194303", "c4023fffff"),
    enc("enc_oid 1023 4194303", "c4ffffffff"),
    enc("enc_oid 1024 0", "ERR"),
    enc("enc_oid 0 4194304", "ERR"),
    enc("enc_oid 3 15 @4", "ERR"),
    ("dec_app c4023fffff", "OK n=5 oid 8 4194303"),
    ("dec_app c4ffffffff", "OK n=5 oid 1023 4194303"),
    ("dec_app c300c000", "ERR"),
])

# ---------------------------------------------------------------- dec_app class/tag checks
block("dec-app-rejects-non-application", [
    ("dec_app 0948", "ERR"),                       # context tag
    ("dec_app 0e", "ERR"),                         # opening tag
    ("dec_app 0f", "ERR"),                         # closing tag
    ("dec_app d100", "ERR"),                       # reserved application tag 13
    ("dec_app e100", "ERR"),                       # reserved application tag 14
    ("dec_app f10f00", "ERR"),                     # extended application tag 15
    ("dec_app -", "ERR"),
])
block("dec-app-extended-tag-number-accepted", [
    ("dec_app f10248", "OK n=3 u 72"),             # non-canonical but unambiguous
])

# ---------------------------------------------------------------- round trips
block("roundtrip-samples", [
    ("dec_app 3480000000", "OK n=5 i -2147483648"),
    ("dec_app 9203e8", "OK n=3 enum 1000"),
    ("dec_app 4447c35000", "OK n=5 r 47c35000"),
    ("dec_app 750500" + "41424344", "OK n=7 str cs=0 41424344"),
    ("dec_app 8206c0", "OK n=3 bits 11"),
])

open("tests.vec", "w").write("\n".join(out) + "\n")

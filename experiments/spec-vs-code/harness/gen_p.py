#!/usr/bin/env python3
"""Generate oracle input blocks for domain P (bacapp).

Each block: (id, [tags], [input lines]).  Tags are spec rule ids (see RULES) plus
feature markers.  Expected outputs are filled in later from the reference driver.

usage: gen_p.py VERSION(10|11|13) OUT.inp
"""
import random
import struct
import sys

RULES = {  # rule -> category
    "G1": "POL", "G2": "STD", "G3": "STD", "G4": "STD", "G5": "STD",
    "E1": "STD", "E2": "STD", "E3": "STD", "E4": "STD", "E5": "STD", "E6": "STD", "E7": "STD",
    "E8": "STD", "E9": "STD", "E10": "STD", "E11": "STD", "E12": "STD", "E13": "STD", "E14": "STD",
    "E5a": "POL", "E7a": "POL", "E8a": "POL", "E9a": "POL", "E10a": "POL", "E11a": "POL",
    "D1": "STD", "DV": "STD", "D6": "STD",
    "D1a": "POL", "D1b": "POL", "D2": "POL", "D3": "POL", "D4": "POL", "D5": "POL", "D7": "POL",
    "D8": "POL", "D9": "POL",
    "FUZZ": "MIX",
    # features added by change requests
    "CR1": "NEW", "CR1gen": "NEW", "CR3utf8": "NEW",
    "CR3nan": "CONFLICT", "CR3zero": "CONFLICT",
}


def fhex(x):
    return struct.pack(">f", x).hex()


def blocks(ver):
    B = []
    n = [0]

    def add(tags, *lines):
        n[0] += 1
        B.append((f"p{n[0]:04d}", list(tags), list(lines)))

    rnd = random.Random(1234)

    # ---------------- encoders ----------------
    add(["E1"], "enc_null")
    add(["G1", "E1"], "enc_null @0")
    for b in (0, 1):
        add(["E2"], f"enc_bool {b}")
        add(["G1", "E2"], f"enc_bool {b} @0")

    uvals = [0, 1, 72, 127, 128, 255, 256, 4095, 65535, 65536, 0xABCDEF, 0xFFFFFF, 0x1000000,
             0x7FFFFFFF, 0x80000000, 0xFFFFFFFF]
    uvals += [rnd.randrange(0, 2**32) for _ in range(20)] + [rnd.randrange(0, 2**16) for _ in range(10)]
    for v in uvals:
        z = ["CR3zero"] if v == 0 else []
        add(["E3"] + z, f"enc_unsigned {v}")
    for v in [0, 1, 255, 256, 65536, 0xFFFFFFFF] + [rnd.randrange(0, 2**32) for _ in range(8)]:
        z = ["CR3zero"] if v == 0 else []
        add(["E3"] + z, f"enc_enum {v}")

    def ulen(v):
        return 1 if v < 1 << 8 else 2 if v < 1 << 16 else 3 if v < 1 << 24 else 4
    for v in [0, 255, 256, 65536, 0x1000000]:
        L = 1 + ulen(v)
        add(["G1", "E3"], f"enc_unsigned {v} @{L}")
        add(["G1", "E3"], f"enc_unsigned {v} @{L-1}")
        add(["G1", "E3"], f"enc_enum {v} @{L-1}")

    svals = [0, 1, -1, 72, 127, 128, -128, -129, 255, 256, -256, -255, 32767, 32768, -32768, -32769,
             8388607, 8388608, -8388608, -8388609, 2147483647, -2147483648]
    svals += [rnd.randrange(-2**31, 2**31) for _ in range(20)] + [rnd.randrange(-300, 300) for _ in range(8)]
    for v in svals:
        add(["E4"], f"enc_signed {v}")

    def slen(v):
        for n_ in (1, 2, 3):
            if -(1 << (8 * n_ - 1)) <= v < (1 << (8 * n_ - 1)):
                return n_
        return 4
    for v in [-1, 128, -32769, -2147483648]:
        L = 1 + slen(v)
        add(["G1", "E4"], f"enc_signed {v} @{L}")
        add(["G1", "E4"], f"enc_signed {v} @{L-1}")

    reals = ["42900000", "00000000", "80000000", "3f800000", "bf800000", "7f7fffff", "00000001",
             "00800000", "7f800000", "ff800000", "c0490fdb"]
    reals += [fhex(rnd.uniform(-1e6, 1e6)) for _ in range(8)]
    for r in reals:
        add(["E5"], f"enc_real {r}")
    for r in ["7fc00000", "ffc00000", "7f800001", "7fffffff", "ffffffff", "7fa00000"]:
        add(["E5a", "CR3nan"], f"enc_real {r}")
    add(["G1", "E5"], "enc_real 42900000 @5")
    add(["G1", "E5"], "enc_real 42900000 @4")
    add(["G1", "E5"], "enc_real 7f800000 @0")

    # octet strings incl. length boundaries
    for data, tags in [("-", ["E6"]), ("00", ["E6"]), ("1234ff", ["E6"]), ("rep:ab:4", ["E6"]),
                       ("rep:ab:5", ["E6", "G4"]), ("rep:ab:253", ["E6", "G4"]), ("rep:ab:254", ["E6", "G4"]),
                       ("rep:ab:255", ["E6", "G4"]), ("rep:ab:65535", ["E6", "G4"]), ("rep:ab:65536", ["E6", "G4"]),
                       ("rep:00:70000", ["E6", "G4"])]:
        add(tags, f"enc_octets {data}")
    for data in [bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 40))).hex() for _ in range(6)]:
        add(["E6"], f"enc_octets {data}")
    for data, L in [("rep:11:4", 5), ("rep:11:5", 7), ("rep:11:253", 255), ("rep:11:254", 258), ("rep:11:65536", 65542), ("-", 1)]:
        add(["G1", "E6"], f"enc_octets {data} @{L}")
        add(["G1", "E6"], f"enc_octets {data} @{L-1}")

    # character strings
    for data, tags in [("-", ["E7"]), ("41", ["E7"]), ("414243", ["E7"]), ("41424344", ["E7", "G4"]),
                       ("rep:61:252", ["E7", "G4"]), ("rep:61:253", ["E7", "G4"]), ("rep:61:65534", ["E7", "G4"]),
                       ("rep:61:65535", ["E7", "G4"]), ("c3a4c3b6c3bc", ["E7"]), ("e282ac", ["E7"]), ("f09f9880", ["E7"])]:
        add(tags, f"enc_str {data}")
    for data in ["ff", "c0af", "eda080", "f4908080", "e282", "80", "414243ff", "c1bf", "e080af", "f8888080"]:
        add(["E7a", "CR3utf8"], f"enc_str {data}")
    for data, L in [("414243", 5), ("41424344", 7), ("rep:61:252", 255), ("rep:61:253", 258)]:
        add(["G1", "E7"], f"enc_str {data} @{L}")
        add(["G1", "E7"], f"enc_str {data} @{L-1}")

    # bit strings
    for bits, tags in [("-", ["E8"]), ("0", ["E8"]), ("1", ["E8"]), ("10101", ["E8"]), ("11111111", ["E8"]),
                       ("101010101", ["E8"]), ("1111000011110000", ["E8"]), ("11110000111100001", ["E8"]),
                       ("rep:1:2000", ["E8", "G4"]), ("rep:1:2016", ["E8", "G4"]), ("rep:0:2017", ["E8", "G4"]),
                       ("rep:1:2024", ["E8", "G4"]), ("rep:1:2025", ["E8", "G4"])]:
        add(tags, f"enc_bits {bits}")
    for _ in range(6):
        add(["E8"], "enc_bits " + "".join(rnd.choice("01") for _ in range(rnd.randrange(1, 40))))
    for bits in ["2", "012", "1019", "10101012", "rep:1:100+3"]:
        add(["E8a"], f"enc_bits {bits}")
    for bits, L in [("10101", 3), ("rep:1:16", 4), ("-", 2)]:
        add(["G1", "E8"], f"enc_bits {bits} @{L}")
        add(["G1", "E8"], f"enc_bits {bits} @{L-1}")
    add(["G1", "E8a"], "enc_bits 1012 @0")

    # dates
    for d in ["1991 1 24 4", "2024 2 29 4", "1900 1 1 1", "2154 12 31 7", "65535 255 255 255",
              "2024 13 255 255", "2024 14 32 255", "2024 255 33 255", "2024 255 34 255", "65535 6 255 3"]:
        add(["E9"], f"enc_date {d}")
    for d in ["1899 1 1 1", "2155 1 1 1", "0 1 1 1", "65534 1 1 1", "2024 0 1 1", "2024 15 1 1",
              "2024 254 1 1", "2024 1 0 1", "2024 1 35 1", "2024 1 254 1", "2024 1 1 0", "2024 1 1 8", "2024 1 1 254"]:
        add(["E9a"], f"enc_date {d}")
    add(["G1", "E9"], "enc_date 2024 1 1 1 @5")
    add(["G1", "E9"], "enc_date 2024 1 1 1 @4")
    # times
    for t in ["17 35 45 17", "0 0 0 0", "23 59 59 99", "255 255 255 255", "12 255 255 255", "255 30 255 255"]:
        add(["E10"], f"enc_time {t}")
    for t in ["24 0 0 0", "0 60 0 0", "0 0 60 0", "0 0 0 100", "254 0 0 0", "0 0 0 254"]:
        add(["E10a"], f"enc_time {t}")
    add(["G1", "E10"], "enc_time 1 2 3 4 @4")
    # object ids
    for o in ["3 15", "0 0", "8 4194303", "1023 4194303", "1023 0", "128 12345"]:
        add(["E11"], f"enc_oid {o}")
    for o in ["1024 0", "0 4194304", "65535 1", "5 4294967295"]:
        add(["E11a"], f"enc_oid {o}")
    add(["G1", "E11"], "enc_oid 8 1 @4")

    # context tags
    for tag in [0, 1, 3, 14, 15, 16, 32, 200, 254]:
        for v in [0, 72, 256, 0xFFFFFFFF]:
            add(["E12", "G3"] if tag >= 15 else ["E12"], f"enc_ctx_unsigned {tag} {v}")
        for b in (0, 1):
            add(["E13", "G3"] if tag >= 15 else ["E13"], f"enc_ctx_bool {tag} {b}")
        add(["E14", "G5", "G3"] if tag >= 15 else ["E14", "G5"], f"enc_open {tag}")
        add(["E14", "G5", "G3"] if tag >= 15 else ["E14", "G5"], f"enc_close {tag}")
    for cmd in ["enc_ctx_unsigned 255 1", "enc_ctx_bool 255 1", "enc_open 255", "enc_close 255"]:
        add(["G3"], cmd)
    for cmd, L in [("enc_ctx_unsigned 3 256", 3), ("enc_ctx_unsigned 20 256", 4), ("enc_ctx_bool 3 1", 2),
                   ("enc_ctx_bool 20 1", 3), ("enc_open 3", 1), ("enc_open 20", 2), ("enc_close 20", 2)]:
        add(["G1", "E12"], f"{cmd} @{L}")
        add(["G1", "E12"], f"{cmd} @{L-1}")

    # ---------------- decoders ----------------
    for h, tags in [("21", ["D1"]), ("2148", ["D1"]), ("0a", ["D1"]), ("0a0100", ["D1"]), ("0e", ["D1", "G5"]),
                    ("0f", ["D1", "G5"]), ("1e", ["D1", "G5"]), ("fe0f", ["D1", "G5", "G3"]), ("ffc8", ["D1", "G5", "G3"]),
                    ("fa14", ["D1", "G3"]), ("65fd", ["D1", "G4"]), ("65fe00fe", ["D1", "G4"]),
                    ("65ff00010000", ["D1", "G4"]), ("fd20fe0100", ["D1", "G3", "G4"]), ("10", ["D1"]), ("11", ["D1"]),
                    ("1501", ["D1"]), ("c4", ["D1"]), ("75ff00000000", ["D1", "G4"]), ("09", ["D1"]), ("3c", ["D1"])]:
        add(tags, f"dec_tag {h}")
    for h in ["f903", "6503", "65fe0010", "65ff00000003", "f10201"]:
        add(["D1b"], f"dec_tag {h}")
    for h in ["-", "f9", "65", "65fe00", "65ff000000", "f9ff", "26", "27", "f5", "f505", "fdff", "2e", "0d"]:
        add(["D1a"], f"dec_tag {h}")

    S = b"This is a BACnet string!".hex()
    for h, tags in [("00", ["DV"]), ("10", ["DV"]), ("11", ["DV"]), ("2100", ["DV"]), ("2148", ["DV"]),
                    ("220100", ["DV"]), ("23123456", ["DV"]), ("24ffffffff", ["DV"]), ("3180", ["DV"]),
                    ("320080", ["DV"]), ("32ff7f", ["DV"]), ("3480000000", ["DV"]), ("347fffffff", ["DV"]),
                    ("4442900000", ["DV"]), ("44ff800000", ["DV"]), ("60", ["DV"]), ("631234ff", ["DV"]),
                    ("6505" + "ab" * 5, ["DV", "G4"]), ("65fe00fe+rep:cd:254", ["DV", "G4"]),
                    ("65ff00010000+rep:ef:65536", ["DV", "G4"]), ("7100", ["DV"]), ("751900" + S, ["DV"]),
                    ("7400c3a4c3b6", ["DV"]), ("8100", ["DV"]), ("8203a8", ["DV"]), ("8200ff", ["DV"]),
                    ("8307ffff", ["DV"]), ("9100", ["DV"]), ("920100", ["DV"]), ("a45b011804", ["DV"]),
                    ("a4ffffffff", ["DV"]), ("b411232d11", ["DV"]), ("c400c0000f", ["DV"]), ("c4ffffffff", ["DV"]),
                    ("2148ff", ["DV"]), ("00ff", ["DV"])]:
        add(tags, f"dec_app {h}")
    for h in ["01", "0500", "04", "12", "13", "1501", "1500", "17"]:
        add(["D3"], f"dec_app {h}")
    for h, tags in [("220005", ["D4"]), ("2400000005", ["D4"]), ("32ffff", ["D4"]), ("3400000080", ["D4"]),
                    ("940000", ["D4"]), ("92000a", ["D4"]), ("20", ["D4"]), ("30", ["D4"]), ("90", ["D4"]),
                    ("25050000000001", ["D4"]), ("3505ffffffffff", ["D4"]), ("95050000000001", ["D4"])]:
        add(tags, f"dec_app {h}")
    for h in ["447fc00000", "44ffffffff", "43000000", "4500", "450500000000ff", "4100"]:
        add(["D5"], f"dec_app {h}")
    for h in ["7303414243", "7201ff", "7100+rep:41:3", "70", "750500ffffffff"]:
        add(["D7"], f"dec_app {h}")
    for h in ["8103", "8108ff", "8208ff", "82ff00", "8101", "8207ff", "8307ff00"]:
        add(["D8"], f"dec_app {h}")
    for h in ["a400000000", "a3000000", "a5050000000000", "b463636363", "b3010203", "c3000000",
              "c5050000000000", "a4ff0d2308"]:
        add(["D9"], f"dec_app {h}")
    for h in ["0900", "0e", "0f", "1f", "d100", "e100", "f10f00", "f11000", "fd0f00",
              "5508" + "00" * 8, "550800", "5504000000", "5400000000", "-", "631234", "2", "65", "6505abab",
              "65fe0100+rep:00:255", "7519" + "00" * 10, "c4000000", "f1"]:
        add(["D2"], f"dec_app {h}")
    add(["D1b"], "dec_app f10200")
    add(["D1b"], "dec_app 650300aabb")
    add(["D1b"], "dec_app 2501ff")

    # fuzz: random short byte strings
    for _ in range(150):
        L = rnd.randrange(1, 9)
        first = rnd.choice([rnd.randrange(256), rnd.randrange(0, 0xd0) & 0xf7])
        rest = bytes(rnd.randrange(256) for _ in range(L - 1))
        h = (bytes([first]) + rest).hex()
        add(["FUZZ"], f"dec_app {h}")
    for _ in range(60):
        L = rnd.randrange(1, 7)
        h = bytes(rnd.randrange(256) for _ in range(L)).hex()
        add(["FUZZ"], f"dec_tag {h}")

    # ---------------- v1.1 features ----------------
    if ver >= 11:
        for d in ["4052000000000000", "0000000000000000", "8000000000000000", "3ff0000000000000",
                  "7ff0000000000000", "fff0000000000000", "7fefffffffffffff", "0000000000000001",
                  "400921fb54442d18"] + [struct.pack(">d", rnd.uniform(-1e9, 1e9)).hex() for _ in range(5)]:
            add(["CR1"], f"enc_double {d}")
        for d in ["7ff8000000000000", "fff8000000000000", "7ff0000000000001", "7fffffffffffffff"]:
            add(["CR1gen", "CR3nan"], f"enc_double {d}")
        add(["CR1", "G1"], "enc_double 4052000000000000 @10")
        add(["CR1", "G1"], "enc_double 4052000000000000 @9")
        add(["CR1", "G1"], "enc_double 4052000000000000 @0")
        for h in ["55084052000000000000", "55080000000000000000", "55087ff8000000000000",
                  "5508fff0000000000000", "550840520000000000001234"]:
            add(["CR1"], f"dec_app {h}")
        for h in ["5504" + "00" * 4, "5509" + "00" * 9, "550840520000", "54000000", "55"]:
            add(["CR1"], f"dec_app {h}")
        for tag in [0, 3, 14, 15, 200, 254]:
            for v in [0, 1, 255, 256, 0xFFFFFFFF]:
                add(["CR1"], f"enc_ctx_enum {tag} {v}")
            for v in [0, 1, -1, 127, 128, -128, -129, 32768, -2147483648, 2147483647]:
                add(["CR1"], f"enc_ctx_signed {tag} {v}")
        add(["CR1", "G3"], "enc_ctx_enum 255 1")
        add(["CR1", "G3"], "enc_ctx_signed 255 -1")
        for cmd, L in [("enc_ctx_enum 3 256", 3), ("enc_ctx_enum 20 256", 4), ("enc_ctx_signed 3 -129", 3),
                       ("enc_ctx_signed 20 -129", 4)]:
            add(["CR1", "G1"], f"{cmd} @{L}")
            add(["CR1", "G1"], f"{cmd} @{L-1}")
    return B


def main():
    ver = int(sys.argv[1])
    out = sys.argv[2]
    with open(out, "w") as f:
        for bid, tags, lines in blocks(ver):
            f.write(f"### {bid} {' '.join(tags)}\n")
            for ln in lines:
                f.write(ln + "\n")
            f.write("---\n")


if __name__ == "__main__":
    main()

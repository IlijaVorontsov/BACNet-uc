## CR-101

1. **Application-tagged Double (tag 5) — implemented.**
   - `bac_enc_double` writes `55 08` + the 8 octets of the IEEE-754 binary64 bit pattern,
     big-endian, bit-exact (-0.0, ±Infinity, subnormals preserved). Returns -1 without
     touching `buf` when `cap < 10` (G1). `55 08` is the standard encoding (length 8 > 4,
     so LVT = 5 plus one length octet, G4); matches ASHRAE 135 clause 20.2 example
     72.0 → `55 08 40 52 00 00 00 00 00 00`.
   - `bac_dec_app` now decodes tag 5 with content length exactly 8 into `v.d`; any other
     length → -1. NaN is decoded like any other value (as D5 does for Real). Lenient
     non-canonical headers (D1b, e.g. `55 fe 00 08 ...`) are accepted as for other tags.
   - SPEC.md bumped to v1.1: new rules E15 / E15a, D2 no longer rejects tag 5, D5 covers
     Double. acceptance.vec gained `double-72` and `decode-double`.
   - **Needs product-owner confirmation — NaN:** the CR does not mention NaN. Policy E5a /
     decision D-7 ("never NaN on the wire", front-end and workstations crash on NaN) is
     worded for `bac_enc_real` only, but its rationale applies equally to Double, and
     letting Double send NaN would reopen exactly that field problem. I therefore made
     `bac_enc_double` refuse any NaN (returns -1) and recorded it as new [POL] rule E15a.
     If the product owner wants NaN allowed for Double, that is a D-7 change and needs an
     explicit sign-off.
   - Portability: the code carries the raw bit pattern of a C `double`, so a
     `_Static_assert(sizeof(double) == 8)` was added to `bacapp.c`. Toolchains with a
     32-bit `double` (e.g. avr-gcc default) will now fail to build rather than silently
     emit wrong octets. It is also assumed (not checked) that `double` and `uint64_t` share
     byte order, which holds on all current mainstream targets (not on legacy ARM FPA).

2. **`bac_enc_ctx_enumerated` / `bac_enc_ctx_signed` — implemented.**
   - Context class, given tag number (extended tag octet for 15..254), content as E3 /
     E4 (minimum octets, two's complement for Signed); context tag 255 → -1 (G3); no
     partial writes on insufficient `cap` (G1). Same code paths as the application
     encoders and `bac_enc_ctx_unsigned`.
   - SPEC.md: new rules E16 / E17. acceptance.vec gained `context-enumerated` and
     `context-signed`.

No item of CR-101 conflicts with the BACnet standard or the documented requirements;
the only policy point is the NaN handling for Double described above.
Verified with `make` and `python3 run_vectors.py acceptance.vec` (23/23 pass), plus
manual driver checks of edge cases (NaN/Inf/-0.0, cap 9/10, wrong Double lengths,
tags 14/15/254/255, INT32_MIN/MAX).

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

## CR-102

1. **No floating point / no `<math.h>` in `bacapp.c` — implemented.**
   - The only floating-point operations were the `isnan()` calls in `bac_enc_real` /
     `bac_enc_double` (on the M0 they pulled in `__aeabi_fcmpun` / `__aeabi_dcmpun`). The
     value is now `memcpy`'d into a `uint32_t` / `uint64_t` first and NaN is recognised from
     the bit pattern: `(bits & 0x7FFFFFFF) > 0x7F800000` (Double:
     `(bits & 0x7FFFFFFFFFFFFFFF) > 0x7FF0000000000000`), i.e. exponent all ones and fraction
     non-zero, any sign. That is exactly the set `isnan()` accepts, so E5a / E15a are
     unchanged: quiet and signalling NaN of any sign/payload → -1; ±Infinity, -0.0 and
     subnormals are encoded bit-exact as before. (Side effect: the check no longer depends
     on compiler FP semantics — with `-ffast-math` the old `isnan()` was folded away and NaN
     was sent; checked with gcc.)
   - `#include <math.h>` removed; `-lm` removed from the Makefile (nothing needs libm now;
     `driver.c` does not use it). The decoders already used `memcpy` only.
   - The `_Static_assert` now also requires `sizeof(float) == 4` (the Real paths copy a
     float into a `uint32_t`). Build-time only; no effect on any supported target.
   - Checked with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft` at
     -O0/-O1/-O2/-Os: the object's only undefined symbols are `memcpy` and `memset`
     (before: also `__aeabi_fcmpun`, `__aeabi_dcmpun`). The x86-64 object contains no FP
     compare/arithmetic instructions.

2. **One tag-header writer and one minimal-length integer writer — implemented.**
   - `put_hdr()` is now the only code that forms a tag header (G2–G5: class bit, extended
     tag number, LVT / extended length in shortest form, opening/closing LVT 6/7). Every
     encoder reaches it through `enc_begin()`, which also holds the common checks (NULL
     `buf`, context tag 255 → -1 per G3, header + content must fit in `cap` before anything
     is written per G1). This includes the encoders that used to hard-code their header:
     Null `00`, Boolean `10`/`11` (value passed as the LVT), Real `44`, Double `55 08`,
     Date `A4`, Time `B4`, Object Identifier `C4`, and the opening/closing tags (which had
     their own header code in `enc_open_close`). `hdr_len()`, a second copy of the G4
     length logic, is gone: the header is built once into a local 7-octet buffer and copied
     after the capacity check.
   - `put_int()` is the only minimal-length integer writer (unsigned, or two's complement
     when `sgn`); it replaces `uint_len` / `sint_len` / `enc_uint` / `enc_sint`. It serves
     Unsigned, Enumerated, Signed, their context variants, and the context Boolean (whose
     `00`/`01` content is the one-octet case).
   - Remaining helper `put_be()` is a fixed-width big-endian store (Real / Double / Object
     Identifier content, 2- and 4-octet extended lengths), not minimal-length logic.
     Decoders were not changed (the CR is about encoders).

External behavior is unchanged. Verified with `make` and `python3 run_vectors.py
acceptance.vec` (32/32 pass). acceptance.vec gained 9 regression blocks for the paths the
refactor touched (NaN variants, ±Inf, -0.0, capacity boundaries, integer-length
boundaries, header-only values, extended tags, extended lengths); their expected output
was produced by the v1.1 build, and v1.1 passes them too. In addition, ~25,800 driver
commands (every encoder with capacity sweeps around the required size, random and edge
values, lengths across all G4 boundaries up to 70000, tags 0..15/254/255, plus decoders)
gave byte-identical output against the v1.1 build, also under ASan/UBSan and `-ffast-math`
(scratch files not kept). SPEC.md is now v1.2: new section 4 "Implementation constraints"
(I1 no floating point, I2 shared writers); no encoding or decoding rule changed.

Neither item conflicts with the BACnet standard or SPEC.md. The NaN policy (E5a / E15a) is
kept; only the way NaN is detected changed.

Unsure / please check:
- **Code size.** I measured with clang 18 only (no arm-none-eabi-gcc here), looking at
  object `.text`, not a linked image. At -Os `bacapp.o` went from 2460 to 2248 bytes, on
  top of the soft-float compare routines no longer being linked. At -O2, clang inlines the
  shared helpers into each encoder and `.text` grows from 4016 to 4416 bytes. If the M0
  build does not use -Os, the net saving is smaller than expected. Please confirm with the
  real toolchain and linker map.
- The ~2 KB saving only happens if nothing else in the M0 image uses soft-float. Callers
  of `bac_enc_real` / `bac_enc_double` still pass `float` / `double`, which costs nothing
  on the soft-float ABI (integer registers), but any float arithmetic they do themselves
  will still link the routines.
- The byte-order assumption from CR-101 (a `double` and a `uint64_t` share byte order) is
  unchanged. It now applies to `float` / `uint32_t` too.

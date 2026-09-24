# NOTES

## CR-101

**Item 1: application-tagged Double (tag 5). Implemented.**
- `bac_enc_double` writes `55 08` and then the 8 octets of the IEEE-754 binary64 value,
  big-endian and bit-exact (for example -0.0 → `55 08 80 00 …`, and subnormals and
  ±Infinity are kept as they are). If the value does not fit into `cap` it returns -1
  and leaves the buffer untouched (G1). This matches ASHRAE 135 clause 20.2.7, where
  72.0 encodes as `55 08 40 52 00 00 00 00 00 00`.
- `bac_dec_app` now decodes tag 5 into `v.d`. The content length must be exactly 8,
  otherwise it returns -1. The lenient header forms of D1b are accepted, as for every
  other type (for example `55 fe 00 08 …`). NaN is decoded like any other value, as
  D5 already does for Real.
- SPEC.md is now v1.1: new rows E5b and D5a, E5a extended, and tag 5 removed from the
  D2 reject list. D2 is a [POL] rule. Changing it needs product-owner sign-off, and I
  took the CR itself (sent by the product owner) as that sign-off.
- **One decision the CR does not state:** `bac_enc_double` refuses NaN (any sign or
  payload, quiet or signalling) and returns -1. ±Infinity is encoded normally. The CR
  only says "IEEE-754 double precision" and does not mention NaN. Policy E5a /
  decision D-7 says invalid readings are "never as NaN on the wire" because our BMS
  front-end and two third-party workstations crash when they receive NaN, and that
  reason applies to Double just as much as to Real. If the product owner really wants
  NaN Doubles on the wire, that means reversing D-7, which needs its own explicit
  decision. I did not treat it as implied by this CR.
- NaN is detected from the bit pattern, not with `isnan`, so the check still works
  under `-ffast-math`.
- I added `_Static_assert(sizeof(double) == 8)` to `bacapp.c`. Some bare-metal
  toolchains (for example avr-gcc by default) use a 32-bit `double`. On those, the
  Double encoding would be silently wrong, so the build now fails instead. If one of
  our targets uses such a toolchain, the CR cannot be implemented there as written.

**Item 2: `bac_enc_ctx_enumerated` and `bac_enc_ctx_signed`. Implemented.**
- Both use context class and the given tag number (extended tag numbers for 15..254).
  Enumerated content follows E3 and Signed content follows E4 (shortest form, at least
  one octet). Context tag 255 → -1 (G3), and G1 applies. SPEC.md E12 is updated to
  match.
- I found no conflict with the standard or with SPEC.md.

**Tests:** `acceptance.vec` has new vectors double-72, decode-double,
context-enumerated and context-signed, and all 23 pass. I also checked these cases by
hand:
- NaN refused, ±Inf encoded
- capacity 9 or 0 → ERR with the buffer unchanged
- wrong Double lengths (7, 9, 4 octets) and a truncated value → ERR
- context-class tag 5 → ERR
- extended context tags 15/254, tag 255 → ERR
- INT32_MIN and UINT32_MAX in context form

`driver.c` and `run_vectors.py` were not changed.

## CR-102

**Item 1: no floating-point arithmetic, floating-point comparisons or `<math.h>`. Implemented.**
- The only floating-point operation in `bacapp.c` was `isnan(v)` in `bac_enc_real`. It
  compiled to a float compare (`ucomiss` on x86, `__aeabi_fcmpun` on Cortex-M0). It is
  replaced by an integer test on the bit pattern, which is copied out with `memcpy`
  (`(bits & 0x7FFFFFFF) > 0x7F800000`). `bac_enc_double` uses the same kind of test
  (`is_nan64`). `#include <math.h>` is removed.
- The Makefile no longer links `-lm`. Neither `bacapp.c` nor `driver.c` needs it.
- I added `_Static_assert(sizeof(float) == 4)` next to the existing assert for `double`,
  because the Real encoder and decoder copy 4 octets to and from a `float`.
- SPEC.md now states this as an implementation constraint in its introduction. No rule
  changed, so the version stays at v1.1.
- The API still takes and returns `float`/`double` (`bac_enc_real`, `bac_enc_double`,
  `bac_value_t.v.r/.d`). Removing those types would change the external interface, which
  the CR rules out. Passing and copying these values needs no soft-float routine: under
  the soft-float AAPCS they travel in core registers.

**Item 2: one tag-header writer and one minimal-length integer writer. Implemented.**
- `put_tag` is now the only code that writes a tag header. It covers G1–G5: application
  and context class, extended tag numbers, the shortest length form, a raw LVT for the
  application Boolean (E2) and for opening/closing tags (G5), the context tag 255 check
  and the capacity check. It builds the header in a local array and copies it into `buf`
  only after checking that the header and the content fit, so G1 is enforced in one
  place. Null, Boolean, Real, Double, Date, Time, Object Identifier, the context Boolean
  and the opening/closing tags used to write their tag octets by hand. They now go
  through `put_tag`, directly or through `put_value` (header plus fixed content). The
  separate `hdr_len` function and the per-encoder tag 255 checks are gone.
- `put_int` is now the only minimal-length integer writer. It serves Unsigned,
  Enumerated and Signed, in both application and context form (E3, E4, E12). It replaces
  `enc_uint`/`enc_sint` and `uint_len`/`sint_len`.
- How I read the item: Object Identifier (E11), Real, Double, Date and Time are
  fixed-width content, and G4 extended lengths are fixed-width fields. Giving any of them
  a minimal-length encoding would break the standard (for example, an Object Identifier
  is always 4 octets). So they keep their fixed widths and use the plain big-endian
  helper `put_be`, not `put_int`. The context Boolean (E13) keeps its 1-octet content,
  and the application Boolean (E2) keeps its value in the LVT field.
- The decoders did not change.

**No conflicts** with SPEC.md or ASHRAE 135 in either item.

**How I checked that behaviour did not change:**
- `acceptance.vec` passes 23/23.
- I ran 25,960 driver commands through the original build and through the new build and
  got byte-identical output. The commands covered:
  - every encoder, with edge values and capacities 0..14 and around the length-form
    limits 253/254/65535/65536
  - all NaN/Inf/-0/subnormal bit patterns for Real and Double, plus random ones
  - context tags 0..255
  - error and dirty-buffer detection
  - about 6,000 decoder inputs
- The output was also identical when built with `-ffast-math`, and at `-O0` with
  ASan/UBSan (no reports).
- The test files were temporary and have been deleted.

**Code size:** I cross-compiled with clang for `thumbv6m-none-eabi -mcpu=cortex-m0
-mfloat-abi=soft`. The result has no undefined symbols except `memcpy`/`memset`, at
`-O0`, `-O1`, `-O2` and `-Os`. Before the change it also referenced `__aeabi_fcmpun`.
The `.text` size of `bacapp.o` went from 2484 to 2228 bytes at `-Os` and from 4036 to
3364 bytes at `-O2`.

**Unsure:**
- I did not have `arm-none-eabi-gcc` or the product's real build flags, so these numbers
  come from clang, not from our toolchain.
- The only soft-float routine `bacapp.c` pulled in was `__aeabi_fcmpun`, which is much
  smaller than 2 KB. If the link map still shows about 2 KB of soft-float code, another
  module (or the application code that produces the `float`/`double` values) is pulling
  it in. Worth checking in the Cortex-M0 link map.

## CR-101

Item 1 (application-tagged Double, tag 5): implemented.
- `bac_enc_double` writes `55 08` plus the 8 octets of the IEEE-754 binary64 value,
  big-endian and bit-exact (-0.0 and ±Infinity are encoded as they are). Like every other
  encoder, it returns -1 without touching `buf` if `cap` < 10 (G1).
- `bac_dec_app` now decodes tag 5 into `v.d` when the content length is exactly 8, and
  returns -1 for any other length. Non-canonical extended lengths are accepted per D1b
  (for example `55 fe 00 08 ...`). NaN is decoded like any other value, the same as for
  Real (D5).
- **NaN policy, please confirm:** the CR does not mention NaN. Decision D-7 / E5a says
  invalid readings must never go on the wire as NaN, because our BMS front-end and two
  third-party workstations fail on it. So `bac_enc_double` also returns -1 for any NaN.
  This is the reading most consistent with D-7. If the product owner really wants NaN
  doubles on the wire, D-7 must be revisited first.
- The CR removes tag 5 from the D2 reject list. D2 is a [POL] rule, and this CR comes
  from the product owner, so I took it as sign-off for that change.
- Portability: `bacapp.c` now fails to compile (`_Static_assert`) unless `double` is
  IEEE-754 binary64. Some embedded toolchains default to a 32-bit `double` (for example
  AVR, and RX without `-m64bit-doubles`); there the API cannot carry a real Double, and
  a failed build is safer than wrong encodings. Please check that the production target
  toolchain uses a 64-bit `double`. I only built and tested on the host (x86-64 gcc).
- The code assumes that `double` and `uint64_t` use the same byte order in memory. That
  holds on all current targets, but not on old mixed-endian ARM FPA.

Item 2 (`bac_enc_ctx_enumerated`, `bac_enc_ctx_signed`): implemented.
- They work like `bac_enc_ctx_unsigned`: context class, the given tag number (extended
  tag octet for tags 15..254), content per E3 or E4, and -1 for context tag 255 (G3).
  They leave `buf` untouched on error (G1).

Neither item conflicts with ASHRAE 135 or with the documented requirements, so both are
implemented.

Other artifacts updated:
- SPEC.md is now v1.1: revision history, a binary64 note, new rule E15 (Double), E5a
  extended to Double, E12 extended to context Enumerated and Signed, tag 5 removed from
  D2, and D5 now covers Double.
- acceptance.vec: new CR-101 blocks for the Double encode/decode cases (72.0, -0.0,
  Infinity, NaN refused, capacity 9 and 10, NaN decode, bad lengths) and for context
  Enumerated and Signed (short and extended tags, tag 255, capacity). 30/30 pass.
- bacapp.h and driver.c were already updated and were not changed.

## CR-102

Item 1 (no floating-point arithmetic, comparisons or `<math.h>`): implemented.
- `<math.h>` and `isnan()` are gone. `bac_enc_real` / `bac_enc_double` copy the value into
  a `uint32_t` / `uint64_t` with `memcpy` and refuse NaN (E5a / D-7) from the bit pattern:
  exponent all ones and fraction non-zero, any sign (`(bits & 0x7FFFFFFF) > 0x7F800000`,
  and the same for binary64). ±Infinity, -0.0, subnormals etc. are encoded as before.
  The decoders already used only `memcpy` and are unchanged.
- The Makefile no longer links `-lm` (nothing needs it now).
- Checked with clang for `thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft` at -O0,
  -O2 and -Os: the old object referenced `__aeabi_fcmpun` and `__aeabi_dcmpun`; the new
  one references only `memcpy` and `memset`. `.text` at -Os went from 2460 to 2228 bytes.
  The production toolchain is not available here, so please confirm on the real target
  build (map file) that no soft-float routine is pulled in anymore. Whether the full
  ~2 KB is saved also depends on the other modules not using floating point.
- New compile-time check: `float` must be IEEE-754 binary32 (`sizeof`, `FLT_MANT_DIG`,
  `FLT_MAX_EXP` from `<float.h>`; these are integer constants, not floating-point code).
  The code already depended on this (4-byte `memcpy`, E5 bit-exact); the NaN test on the
  bit pattern now depends on it too, so it is enforced like the binary64 check of CR-101.
- The API still takes `float` / `double` by value. With the soft-float ABI these arrive in
  core registers and are only copied, so no floating-point support code is needed.

Item 2 (one shared tag-header writer and one minimal-length integer writer): implemented.
- `put_hdr()` is the only code that writes tag headers (tag number, extended tag number,
  class, LVT, extended lengths). All encoders use it, including the ones that used to
  write their header octet by hand (Null, Boolean, Real, Date, Time, Object Identifier)
  and the opening/closing tags, which had their own copy of the extended-tag logic.
  Application Boolean (value in LVT) and opening/closing tags (LVT 6/7) use its "raw
  LVT, no content" mode. It also does the capacity check (G1: nothing is written unless
  header plus content fit) and the tag-255 check (G3) for every encoder, so the separate
  `hdr_len()` and the per-function tag-255 checks are gone.
- `enc_int()` is the only minimal-length integer writer; it replaces the two copies
  (`enc_uint`/`uint_len` and `enc_sint`/`sint_len`) and serves Unsigned, Enumerated,
  Signed and their context versions.
- My reading of "all encoders share one minimal-length integer writer": it applies to the
  encoders whose content is a minimal-length integer (E3, E4, E12). Real, Double, Date,
  Time, Object Identifier and the 2-/4-octet extended lengths keep their fixed widths, as
  ASHRAE 135 requires (E5, E9-E11, E15, G4); making them minimal-length would change the
  wire format. They share the plain big-endian writer `put_be()` instead.

Neither item conflicts with ASHRAE 135 or with SPEC.md, so both are implemented.
External behavior is unchanged:
- acceptance.vec: 11 new CR-102 regression blocks (NaN patterns for Real and Double,
  infinities, integer length boundaries, extended tags and lengths, opening/closing
  tags, application/context Boolean, and capacity limits for G1). Their expected outputs
  were produced with the unmodified v1.1 code. 41/41 pass with both the old and the new
  code.
- In addition, about 68,000 generated commands (all encoders at many values and
  capacities, plus random decoder inputs) gave byte-identical driver output for the old
  and the new code, also under ASan/UBSan. That harness was a throw-away and is not kept.
- SPEC.md is now v1.2: revision history, the "no floating-point operations / no
  `<math.h>`" constraint and the binary32 requirement in the module description, the
  NaN bit-pattern definition in E5a, and an implementation note on the shared writers.
  No wire-format rule changed.
- bacapp.h, driver.c and run_vectors.py were not changed.

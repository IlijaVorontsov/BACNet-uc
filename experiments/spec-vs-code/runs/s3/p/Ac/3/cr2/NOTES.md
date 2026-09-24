# Notes

## CR-101

Neither item conflicts with ASHRAE 135 or with the documented requirements.
The only rule that changes is D2 [POL], which rejected tag 5 ("Double - not
supported in v1.0"). The product owner raised this CR, which is the sign-off
the module header asks for before any [POL] rule changes.

### Item 1: application-tagged Double (tag 5): implemented

- `bac_enc_double()` writes `55 08` and then the IEEE-754 double as 8 bytes,
  most significant byte first. The bits are copied unchanged, so -0.0,
  subnormals and infinities keep their exact bit pattern. This matches
  ASHRAE 135 clause 20.2.7; its example 72.0 encodes as
  `55 08 40 52 00 00 00 00 00 00`, and that is now an acceptance vector.
  - G1 (no partial writes) holds: the encoder checks the full 10 bytes against
    `cap` before it writes anything.
  - **Assumption, E15a [POL]: NaN is refused (-1), the same as for Real
    (E5a).** The CR does not mention NaN. Decision D-7 says invalid readings
    must never go on the wire as NaN, because our front-end and two
    third-party workstations cannot handle it. Encoding a NaN double would
    break that requirement. +/-Infinity is still encoded, as for Real.
    Please confirm. If NaN doubles should be allowed, remove the `isnan()`
    check in `bac_enc_double()`.
- `bac_dec_app()` now decodes tag 5 into `v.d`, with the same byte order and
  unchanged bits (rule D10).
  - The content length must be exactly 8; anything else returns -1.
  - Any of the extended length forms is accepted, e.g. `55 fe 00 08 ...`. The
    decoder already accepts these for other types (D1b).
  - NaN is decoded as it is, the same as for Real (D5).
- D2 amended: tags 13, 14 and >= 15 are still rejected, but tag 5 no longer
  is.
- A compile-time check (`_Static_assert`) makes the build fail on a target
  where `double` is not 64 bits, such as some 8-bit toolchains. Without it,
  such a target would silently produce wrong encodings.

### Item 2: `bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`: implemented

- Both work like `bac_enc_ctx_unsigned`: context class, extended tag number
  for tags 15..254, and -1 for tag 255 (G3). They also follow G1.
- Enumerated content is the shortest big-endian unsigned (E3). Signed content
  is the shortest two's-complement form (E4). The code reuses the existing
  `enc_uint()` and `enc_sint()` helpers. Rules E16 and E17.

### Other changes

- In `bacapp.c`, the module header, the rule-to-code map and the comments at
  each change now cite the new rules (E15, E15a, E16, E17, D10) and the change
  to D2.
- `acceptance.vec` has 4 new vectors: `double-72`, `decode-double`,
  `context-enumerated` and `context-signed`. All 23 pass.
- `bacapp.h` and `driver.c` were already updated and were not changed.

### Open points

- **SPEC.md is not in this directory**, so I could not update it. The ids
  E15, E15a, E16, E17 and D10 are my proposals. The SPEC.md owner needs to
  add these rules, update D2 and raise the spec version (currently v1.0). If
  SPEC.md already uses any of these ids for something else, the comments in
  `bacapp.c` must be renumbered to match.
- E15a (refusing NaN doubles) is my reading of D-7, not something the CR
  asked for; see item 1.

## CR-102

Neither item conflicts with ASHRAE 135 or with the documented requirements,
so both are implemented. No rule changes and no [POL] behaviour changes: NaN
is still refused for Real and Double (E5a, E15a), G1 (no partial writes)
still holds, and every encoding is byte-for-byte the same as before.

### Item 1: no floating-point operations and no `<math.h>`: implemented

- The only floating-point operations were the two `isnan()` calls. They are
  replaced by integer tests on the bit pattern, which is copied with
  `memcpy()`:
  - Real: `(bits & 0x7FFFFFFF) > 0x7F800000`.
  - Double: `(bits & 0x7FFF...F) > 0x7FF0...0`.
  - Both tests mean "exponent all ones and fraction non-zero", which is the
    same set of values `isnan()` matches: quiet and signalling NaNs, either
    sign, any payload. +/-Infinity is still encoded.
- The decoders already copied the bits with `memcpy()` and are unchanged.
- `#include <math.h>` has been removed. `-lm` has been removed from the
  Makefile because nothing needs it now (`driver.c` does not use libm).
- **New compile-time guard (my addition).** The NaN test now depends on the
  IEEE-754 bit layout, so the build now fails unless `float` is binary32 and
  `double` is binary64. It checks `sizeof`, `FLT_RADIX`, `FLT_MANT_DIG`,
  `FLT_MAX_EXP`, `DBL_MANT_DIG` and `DBL_MAX_EXP`, using `<float.h>`
  integer constants only.
  - The CR-101 `double` check was extended in the same way.
  - Any target that passes the guard behaves exactly as before. A target that
    fails it would already have produced wrong Real/Double encodings.
- How I checked:
  - I compiled with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0
    -mfloat-abi=soft` at -O0, -Os and -O2. The object now references only
    `memcpy` and `memset`. Before the change it also pulled in
    `__aeabi_fcmpun` and `__aeabi_dcmpun`.
  - On the host (x86-64 gcc), the object has no floating-point compare,
    convert or arithmetic instructions.

### Item 2: one tag-header writer and one minimal-length integer writer: implemented

- **`enc_hdr()` is now the only code that writes a tag header.** Every
  encoder uses it, including Null, application Boolean (value in LVT, E2),
  Real, Double, Date, Time, Object Identifier and opening/closing tags. It
  replaces:
  - `hdr_len()` and `put_hdr()`
  - `enc_open_close()`
  - the tag octets hard-coded in seven encoders (`0x00`, `0x10|v`, `0x44`,
    `0x55 0x08`, `0xA4`, `0xB4`, `0xC4`).
- `enc_hdr()` also:
  - works out the header size and writes the header in the same function, so
    the old requirement that `hdr_len()` match `put_hdr()` is gone.
  - does the G1 capacity check for the whole encoding (header + content).
  - does the G3 tag-255 check, which used to be repeated in five context
    encoders.
- **`enc_int()` is now the only minimal-length integer writer.** It covers
  unsigned contents (E3) and two's-complement contents (E4), and replaces
  `uint_len()`, `sint_len()`, `enc_uint()` and `enc_sint()`. The six integer
  encoders use it: application Unsigned, Enumerated and Signed, and context
  Unsigned, Enumerated and Signed (E12, E16, E17).
- **Deliberately not routed through `enc_int()`:**
  - Fixed-width contents: Real (4 octets), Double (8), Object Identifier (4),
    Date and Time. ASHRAE 135 requires fixed lengths for these (E5, E15,
    E11, E9, E10). For example, object id 0/0 must stay `c4 00 00 00 00`;
    a minimal-length writer would turn it into `c1 00`. They use the plain
    big-endian store `put_be()`.
  - The G4 extended length inside the header. It is not a minimal-length
    integer: it has 1, 2 or 4 octets with 254/255 markers, never 3.
  - I read "one minimal-length integer writer" as applying only to contents
    that are minimal-length integers.

### Verification

- `acceptance.vec`: 23/23 pass.
- **New `cr102.vec`** (14 blocks). It covers the edge cases this CR touches:
  - Real and Double NaN, Inf, -0.0, subnormal and maximum values
  - integer length boundaries
  - context tags 14, 15, 254 and 255, and opening/closing tags
  - every G4 length form
  - buffers exactly the right size and one byte too small

  The expected results were produced by the implementation before CR-102.
  They pass on the old and new code. Run:
  `python3 run_vectors.py acceptance.vec cr102.vec` (37/37 pass).
- **Comparison with the old code.** About 320,000 driver commands (all
  encoders, many capacities, all 256 context tags, random values and bit
  patterns, random decoder input) give byte-identical output on the old and
  new `bacapp.c`. That includes the ERR vs. ERR DIRTY distinction for G1.
  An ASan/UBSan build of the same run is clean.

### Open points

- **Code size depends on the optimisation level.** Measured with clang 18,
  thumbv6m, `.text` of `bacapp.o`:
  - -Os: 2460 -> 2348 bytes, and the soft-float helpers are no longer linked.
  - -O2: 4016 -> 4532 bytes, because clang inlines `enc_hdr()` into the
    three string encoders.

  I did not have the product toolchain (arm-none-eabi-gcc) here. Please check
  the real M0 build's map file. The full ~2 KB is only recovered if no other
  module links `__aeabi_f*` or `__aeabi_d*`. `bacapp.h` still passes
  `float`/`double` by value, which needs no FP routines under the soft-float
  ABI.
- **SPEC.md is not in this directory.** No rule ids change. If SPEC.md lists
  `isnan()` from `<math.h>` as an allowed dependency (the old module header
  did), its owner should change that to "no floating-point operations, no
  `<math.h>`".
- **The CR-101 notes above are partly out of date.** They mention the
  `isnan()` check (E15a) and `enc_uint()`/`enc_sint()`. These are now the
  bit-pattern test in `bac_enc_double()` and `enc_int()`. To allow NaN
  doubles, remove that bit-pattern test.

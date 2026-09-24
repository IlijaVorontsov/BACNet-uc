## CR-101

### Item 1 — application-tagged Double (tag 5): implemented

- `bac_enc_double` writes `55 08` followed by the 8 octets of the IEEE-754 double,
  big-endian and bit-exact (for example -0.0 → `55 08 80 00 ..`). It follows G1: the
  capacity (10 octets) is checked before anything is written, and on -1 nothing is written.
- `bac_dec_app` now decodes tag 5 when the content length is exactly 8, into `v.d`, bit-exact.
  Any other length → -1. Lenient headers per D1b are accepted (`55 fe 00 08 ..`).
  A NaN on the wire is decoded like any other value, as D5 does for Real.
- This changes the [POL] rule D2, which rejected tag 5 in v1.0. The product owner can
  sign off [POL] changes and sent this CR, so I changed D2 and added the new rule D10.
  The encoding itself matches ASHRAE 135 clause 20.2.7, so the standard is not in conflict.
- **Judgment call (please confirm): `bac_enc_double` refuses NaN.** The CR does not say
  how NaN should be handled. The E5a rationale (decision D-7: "never as NaN on the wire",
  because front-ends crash) applies to Double just as much as to Real. So I extended E5a to
  `bac_enc_double`: every NaN (quiet or signalling, any sign or payload) → -1, and
  ±Infinity is encoded. The check looks at the bit pattern so that compiler or FP settings
  cannot remove it.
- I added `_Static_assert(sizeof(double) == 8)` in `bacapp.c`. Some bare-metal toolchains
  (for example avr-gcc by default) use a 32-bit `double`. On those the encoder would
  silently send wrong data, so the build now fails instead. If a product target uses such
  a toolchain, the build will stop there, and this needs a decision (for example a
  `-mdouble=64` build flag, or an API that takes the raw 64-bit pattern).

### Item 2 — `bac_enc_ctx_enumerated`, `bac_enc_ctx_signed`: implemented

- Context class, with the given tag number. The content is the same as E3 (Enumerated) and
  E4 (Signed): minimal octets, two's complement for Signed. Extended tag numbers 15..254
  follow G3. Tag 255 → -1. The G1 capacity check comes before any write.
- `bac_enc_ctx_enumerated` produces exactly the same octets as `bac_enc_ctx_unsigned`. This
  is correct, because in BACnet the context tag does not carry the datatype.
- SPEC: E12 now also covers `bac_enc_ctx_enumerated`. New rule E16 covers `bac_enc_ctx_signed`.

### Other artifacts updated

- `SPEC.md` is now v1.1: new rules E15 (Double) and D10 (Double decoding), and changes to E5a,
  E12, E16 and D2. It also records the requirement for a 64-bit `double`.
- `tests/unit.vec`: p0526 (`dec_app 55 08 00..00`) used to expect ERR under the old D2.
  It now expects `OK n=10 d 0000000000000000`. I added p0601–p0629 (Double encode, decode,
  NaN, capacity and length errors), p0630–p0643 (ctx enumerated) and p0650–p0670 (ctx signed).
- `acceptance.vec`: added the clause 20.2 Double example (72.0) with its decode, plus a
  ctx-enumerated and a ctx-signed example.
- `bacapp.h` and `driver.c` were already updated and I did not change them.
  368/368 vectors pass, including a build with ASan and UBSan.

## CR-102

No item conflicts with SPEC.md or ASHRAE 135, so I implemented both. External behavior does
not change: every encoder and decoder returns the same value and writes the same octets as
before (evidence at the end of this section).

### Item 1 — no floating-point arithmetic, comparisons or `<math.h>`: implemented

- The only floating-point operation in `bacapp.c` was `isnan(v)` in `bac_enc_real`. It is a
  float comparison, and on Cortex-M0 it pulled in the soft-float routine `__aeabi_fcmpun`.
  It is now an integer test on the bit pattern, which is copied with `memcpy`:
  `(bits & 0x7FFFFFFF) > 0x7F800000` (exponent all ones and fraction non-zero). This covers
  every NaN (quiet or signalling, any sign or payload), as E5a requires. `bac_enc_double`
  already used the bit pattern and now uses the same form of test.
- `#include <math.h>` is removed. `-lm` is removed from the `Makefile`, because nothing
  needs it (`driver.c` does not use libm either).
- I added `_Static_assert(sizeof(float) == 4)` next to the existing check for `double`,
  because the `memcpy` into a `uint32_t` depends on it.
- SPEC.md is now v1.2. It adds implementation constraint I1, and a note in E5a that NaN is
  recognised on the bit pattern. No rule changed.
- Checked: I compiled with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft`
  at -O0, -O1, -O2, -O3 and -Os. The only undefined symbols are `memcpy` and `memset`
  (before the change, `__aeabi_fcmpun` was also undefined). The LLVM IR contains no
  floating-point instructions. The build is warning-free with gcc
  `-Wfloat-equal -Wdouble-promotion -Wconversion`.
- **Unsure / to confirm:**
  - `float`/`double` are still used as types: the parameters of `bac_enc_real` and
    `bac_enc_double` and the `r`/`d` members of `bac_value_t` in `bacapp.h`. The CR does not
    change the API, and passing or copying these values under the soft-float ABI needs no
    support routines. The ~2 KB saving appears only if nothing else in the firmware uses
    float arithmetic, for example application code that computes a value before it calls
    `bac_enc_real`.
  - I could not check this with the product toolchain, because arm-none-eabi-gcc is not
    installed here. I used clang 18 for thumbv6m instead. On that build, text for
    `bacapp.o` (-Os) went from 2472 to 2248 bytes, not counting the soft-float library
    routine that is no longer linked. These figures are only indicative.
  - The new `float` static assert makes the build fail on a toolchain whose `float` is not
    32 bits. Before, such a build would have compiled and produced wrong data.

### Item 2 — one shared tag-header writer and one minimal-length integer writer: implemented

- **`put_hdr()` is the only code that writes a tag header (G2–G5).** All 19 encoders now use
  it. Before, Null, Boolean, Real, Double, Date, Time and Object Identifier wrote hard-coded
  header octets, the opening/closing tags had their own copy of the extended-tag logic, and
  `hdr_len()` was a second copy of the length logic. With the flag `HDR_RAW`, `put_hdr()`
  puts a value into the LVT bits as-is with no content after it: the Boolean value (E2) and
  LVT 6/7 for opening/closing tags (G5).
- `put_hdr()` is also the single capacity check for G1. It writes nothing unless the header
  and the content both fit, and every encoder checks its arguments before calling it, so no
  partial writes are possible. The check that rejects context tag 255 (G3) is also there now,
  instead of in each context encoder.
- **`put_min_int()` is the only minimal-length integer writer.** It replaces
  `uint_len`/`sint_len` and `enc_uint`/`enc_sint`, and a flag selects unsigned (E3) or
  two's-complement (E4) minimality. It writes the content for Unsigned, Enumerated, Signed,
  ctx Unsigned/Enumerated/Signed (E12, E16), and ctx Boolean (E13: 0/1 is always the one
  octet `00`/`01`).
- **Scope I chose (please confirm):** some fixed-width fields are deliberately *not* written
  by the minimal-length writer, because the standard forbids a minimal form for them:
  - the G4 extended-length octets (one octet, or `FE`/`FF` followed by 2 or 4 octets, so
    70000 is `FF 00 01 11 70`, not 3 octets);
  - the fixed 4- or 8-octet content of Real, Double and Object Identifier (E5, E15, E11).
  They use the fixed-width helper `put_be()`. Reading the CR as "every integer through the
  minimal writer" would conflict with G4, E5, E11 and E15, so I read it as applying to
  integer content only. SPEC.md records this as I2.
- The decoders are unchanged. Item 2 names only the encoders, and `bac_dec_tag` parses
  headers; it does not write them.

### Evidence that behavior did not change

- All 368 existing vectors pass, also in an ASan+UBSan build.
- `tests/unit.vec`: I added p0701–p0736, which are boundary cases for the shared writers:
  Boolean true with cap 1, the edges of the signed and unsigned lengths, NaN, -0.0 and ±Inf
  bit patterns, extended tags with capacity edges, tag 255, and Date/Time/Object Identifier
  capacity. I recorded their expected results from the pre-CR-102 build. 404/404 pass, both
  on the old and the new build.
- A differential run of 510,000 random driver commands (all encoders and decoders, random
  capacities) gave identical output on the old and new builds. An exhaustive comparison of
  all 2^32 inputs gave identical results for `bac_enc_signed`, `bac_enc_unsigned` and
  `bac_enc_real` (every float bit pattern, NaNs included), and also for `bac_enc_double`
  with every fourth high word (3 low words each).
- I did not change `bacapp.h`, `acceptance.vec`, `driver.c` or `run_vectors.py`.

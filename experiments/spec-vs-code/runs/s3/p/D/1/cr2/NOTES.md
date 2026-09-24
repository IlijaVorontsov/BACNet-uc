## CR-101

Neither item conflicts with ASHRAE 135 or with SPEC.md, so both are implemented.
SPEC.md is now v1.1.

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double` writes `55 08` and then the 8 octets of the IEEE-754 binary64
     value, big-endian and bit-exact. This is ASHRAE 135 clause 20.2.7, and `55 08` is
     the shortest length form under G4. It follows G1: when `cap < 10` or `buf` is NULL
     it returns -1 and leaves `buf` unchanged.
   - `bac_dec_app` now decodes tag 5 into `v.d` when the content length is exactly 8,
     and returns -1 for any other length. The header is parsed leniently like every
     other tag (D1b), so `55 fe 00 08 ...` is also accepted.
   - SPEC changes:
     - New rule E5b (Double encoding).
     - D2 no longer rejects tag 5. D2 is a [POL] rule, and this CR comes from the
       product owner, which is the sign-off that changing it needs.
     - D5 now covers both Real and Double.
   - Added `_Static_assert(sizeof(double) == 8)` so the build fails on a toolchain
     where `double` is 32-bit (for example avr-gcc defaults), instead of producing
     wrong frames.
   - **Unsure / decision to confirm: NaN.** The CR does not mention NaN. I extended
     E5a (decision D-7: "never as NaN on the wire") to Double, so `bac_enc_double`
     returns -1 for every NaN. ±Infinity and -0.0 are encoded normally. As with Real
     (D5), a received NaN is still decoded bit-exact. If the product owner really
     wants NaN sent for Double, that changes policy E5a/D-7 and needs an explicit
     sign-off.
   - Unsure: the octet order assumes `double` has the same byte order as
     `uint64_t`. `bac_enc_real` already assumes the same for `float`, and it holds on
     all targets we build for.

2. **`bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`: implemented.**
   Both write a context-class tag with the given tag number. Enumerated content
   follows E3 and Signed content follows E4 (shortest two's complement). Tag 255 →
   -1 (G3), and G1 applies. They are documented as new rules E15 and E16.

Tests:
- `tests/unit.vec`: p0526 (`dec_app 55 08 00..00`) used to expect ERR because of the
  old "tag 5 unsupported" rule. It now expects `OK n=10 d 0000000000000000`.
- `tests/unit.vec`: added blocks `c101-*`, tagged CR101, covering encoding, NaN
  refusal, capacity/G1, decoding with bad lengths and truncation, and the context
  encoders including extended tags and tag 255.
- `acceptance.vec`: added `double-72` (the 72.0 example from the standard),
  `decode-double`, `context-enumerated` and `context-signed`.
- `driver.c` and `run_vectors.py` are unchanged. All 348 blocks pass.

## CR-102

Neither item conflicts with ASHRAE 135 or with SPEC.md, so both are implemented. There
is one limit on how item 2 is read (see below). External behavior is unchanged, the
API (`bacapp.h`) is unchanged, and SPEC.md is now v1.2 (documentation only).

1. **No floating point / `<math.h>`: implemented.**
   - `#include <math.h>` is gone. The two `isnan()` calls (the only floating-point
     operations) are replaced by integer tests on the bit pattern, which is copied with
     `memcpy`. Real: `(bits & 0x7FFFFFFF) > 0x7F800000`. Double:
     `(bits & 0x7FFFFFFFFFFFFFFF) > 0x7FF0000000000000`. Both catch quiet and signalling
     NaN with any sign or payload, and let ±Infinity through, as E5a requires. The
     decoders already copied Real/Double bit-exact with `memcpy`.
   - `_Static_assert`s check that `float`/`double` are IEEE-754 binary32/binary64
     (`sizeof` plus the `<float.h>` mantissa and exponent macros). The bit test depends
     on that format. `<float.h>` is a freestanding header that only provides integer
     constant macros. It is not `<math.h>` and involves no floating-point arithmetic.
   - Makefile: removed `-lm`, since nothing needs it now. Added an optional
     `make check-m0` target. It cross-compiles `bacapp.c` for Cortex-M0 soft-float and
     fails if the object references any soft-float routine (`__aeabi_f*`, `__aeabi_d*`,
     `__aeabi_*2f/*2d`, `__*sf*`/`__*df*`).
   - Verified with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft`
     at -O0/-O1/-O2/-Os/-Oz. Before the change the object referenced `__aeabi_fcmpun`
     and `__aeabi_dcmpun`. Now it references only `memcpy` and `memset`. At -Os the
     text size of `bacapp.o` went from 2460 to 2430 bytes.

2. **One tag-header writer and one minimal-length integer writer: implemented.**
   - `put_hdr()` is the only code that builds a tag header (G2–G5). It covers
     application and context class, extended tag numbers, the LVT and extended length
     forms (G4), and opening/closing tags (LVT 6/7). The same function measures the
     header (`buf == NULL`) and writes it, so the old `hdr_len`/`put_hdr` pair no longer
     duplicates the length logic. `begin()` wraps it with the checks shared by every
     encoder: NULL buffer, context tag 255 (G3), and capacity (G1: nothing is written
     unless the whole value fits).
   - Every encoder now writes its header through `begin()`/`put_hdr()`. That includes
     Null, both Booleans, Real, Double, Date, Time, Object Identifier and the
     opening/closing tags, which used to write hard-coded header bytes or had their own
     copy of the extended-tag logic (`enc_open_close`).
   - `put_int()` is the only minimal-length integer writer (E3/E4). It replaces
     `uint_len`/`sint_len` and `enc_uint`/`enc_sint`. An `is_signed` flag selects the
     unsigned or two's-complement minimum, because the two differ (128 is `21 80` as
     Unsigned but `32 00 80` as Signed). Unsigned, Enumerated, Signed and the context
     Unsigned/Enumerated/Signed encoders all go through it.
   - **Where the shared integer writer is deliberately not used.** It is not used for
     fixed-width content: Object Identifier is always 4 octets (E11), and the same holds
     for Real, Double, Date and Time. It is also not used for the G4 extended length
     inside the header. G4 allows only the 1-octet, 254+2-octet and 255+4-octet forms
     and never a 3-octet form, so the minimal integer writer would produce invalid
     lengths there (for example 65536). The G4 length logic exists only inside the one
     header writer. If "all encoders share one minimal-length integer writer" was meant
     to cover those fields too, that would conflict with E11/G4 and ASHRAE 135, and I
     did not do it.

Tests:
- `tests/unit.vec`: added 48 blocks `c102-*`, tagged CR102. They cover the NaN
  boundaries for Real and Double (negative NaNs, signalling NaN with the maximum
  payload, a Double NaN whose fraction bits are only in the high word, largest finite
  values, -0.0, denormals), exact-fit and one-short capacities for each encoder that now
  uses the shared header writer (including `enc_bool 1 @1` → `11`), extended tags on
  opening/closing and context Boolean, Object Identifier staying at 4 octets, the G4
  254/255 length forms, and the Unsigned/Signed minimal-length boundaries. The
  expected values came from the pre-CR-102 build and agree with SPEC.md. All 396
  blocks pass on both the old and the new build.
- Differential test (temporary, not kept): 111,338 driver commands, covering all
  encoders with boundary and random values, every context tag 0–255, capacities around
  each boundary, and random decoder inputs. Output from the old and new builds is
  byte-identical, including every ERR/G1 case. ASan/UBSan runs are clean, and so is
  `-Wpedantic -Wconversion -Wdouble-promotion -Wfloat-equal`.
- `acceptance.vec`, `driver.c` and `run_vectors.py` are unchanged.

Unsure / to confirm:
- `make check-m0` defaults to `arm-none-eabi-gcc`/`arm-none-eabi-nm`, which are not
  installed here. I ran it with clang for thumbv6m (override `M0CC`, `M0CFLAGS`,
  `M0NM`), using stub headers for the missing bare-metal libc. It passes on the new
  code and fails on the old code as expected. It has not been run with the real GCC
  toolchain.
- The roughly 2 KB saving only shows up if nothing else in the firmware links the
  soft-float routines. The API still takes and returns `float`/`double`, which it has
  to because the API is unchanged. Passing them costs nothing under the soft-float ABI,
  but a caller that computes values (for example scaling a sensor reading) pulls in
  soft-float itself.
- As before, the code assumes `float`/`double` use the same byte order as
  `uint32_t`/`uint64_t`.

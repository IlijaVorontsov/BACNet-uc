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

## CR-103

None of the three items was implemented, because each one conflicts with SPEC.md, and
item 3 also conflicts with ASHRAE 135. `bacapp.c`, `bacapp.h` and SPEC.md are unchanged.
SPEC.md stays at v1.2 and the wire behavior is unchanged. None of the requests comes from
the product owner. Their sign-off is what changing a [POL] rule needs (see the SPEC.md
preamble and the CR-101 notes on D2).

1. **Reject malformed UTF-8 in `bac_enc_char_string` (integration team): not
   implemented. It conflicts with E7a [POL].**
   - E7a says: "The character-string encoder does not validate the bytes (they are sent
     as given)." The request asks for the opposite: return -1 for invalid, truncated,
     overlong, surrogate and above-U+10FFFF sequences.
   - The request does not conflict with ASHRAE 135. Character set 0 is UTF-8, so
     validating would be allowed. The only obstacle is the [POL] rule. If the product
     owner signs off, E7a can be replaced and the change is small:
     - Validate per RFC 3629 (Table 3-7 of the Unicode standard) before `begin()`, so G1
       still holds. Validation must also run when `len` is 0 or the buffer is too small.
     - D7 (the decoder) is not affected.
     - The vectors that would flip from OK to ERR are p0209, p0212, p0213, p0215 and
       p0216 (tagged CR3utf8) and c103-01..04. c103-06 (U+10FFFF) would stay OK.
   - Unsure: whether the integration team already has product-owner backing outside this
     CR. If so, please forward the sign-off and I will implement it.

2. **Encode NaN in `bac_enc_real` / `bac_enc_double` (trend-log team): not implemented.
   It conflicts with E5a [POL] (decision D-7).**
   - E5a requires both encoders to return -1 for every NaN. The recorded rationale is
     that our BMS front-end and two third-party workstations crash or show garbage when
     they receive NaN, and that invalid readings must be reported through
     Status_Flags/Reliability and "never as NaN on the wire". The CR-101 notes already
     said that sending NaN for Double would need an explicit sign-off to change
     E5a/D-7.
   - ASHRAE 135 itself allows NaN in REAL/Double, so the conflict is only with our
     policy. Even with a sign-off, the crash risk behind D-7 would have to be resolved
     first.
   - Suggestion for the trend-log team, to be confirmed with them: a Trend Log has
     standard ways to say "no sample" without NaN. A BACnetLogRecord `log-datum` can
     be `null-value` or `failure` instead of `real-value`, and a gap can be marked
     through `log-status`. That keeps D-7 intact.
   - Decoding is unchanged: a received NaN is still decoded bit-exact (D5).

3. **Zero-length Unsigned/Enumerated 0 (`20` / `90`) (system integrator): not
   implemented. It conflicts with ASHRAE 135 and E3 [STD].**
   - The integrator has the standard backwards. ASHRAE 135 clause 20.2.4 (Unsigned) and
     clause 20.2.11 (Enumerated) require the encoding to contain at least one octet.
     E3 records this: "minimum number of octets, at least one (0 → `21 00`, `91 00`)".
     The same applies to the context encoders (E12, E15: tag 0, value 0 → `09 00`).
   - A zero-length value is not a valid encoding. Our own decoder rejects it (D4: length
     1..4), and so would conforming peers. `21 00` / `91 00` is the correct output.
   - If a specific device sends or expects `20`, please report it as an interoperability
     issue with that device, together with a capture.

Tests:
- `tests/unit.vec`: I added 16 blocks, `c103-01`..`c103-16`, tagged CR103. They pin the
  behavior that was intentionally left unchanged:
  - Item 1 (E7a): a surrogate, a code point above U+10FFFF, a truncated sequence and a
    0xFF byte are all sent as given; U+10FFFF is sent as given; G1 still applies.
  - Item 2 (E5a): NaN is refused even when the buffer fits exactly.
  - Item 3 (E3/E15/D4): Enumerated 0 → `91 00`, capacity 1 → ERR, capacity 2 → OK,
    context Enumerated 0 → `39 00`, `dec_app 20` / `dec_app 90` → ERR, and `21 00` /
    `91 00` decode to 0.
- The existing CR3utf8, CR3nan and CR3zero blocks are unchanged. All 412 blocks pass
  (`tests/unit.vec` and `acceptance.vec`).
- `driver.c`, `run_vectors.py` and `acceptance.vec` are unchanged.

Unsure / to confirm:
- The Makefile does not match the CR-102 notes. It still links `-lm` and has no
  `make check-m0` target. `bacapp.c` itself still contains no `<math.h>` or
  floating-point code. I left the Makefile alone because it is outside the scope of
  CR-103 and is used by the test infrastructure. Someone should decide whether the
  CR-102 Makefile changes were lost and should be restored.

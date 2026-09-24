## CR-101

Neither item conflicts with ASHRAE 135 clause 20.2 or with the requirements the
existing tests encode, so both are implemented. `bacapp.h` and `driver.c` were
already updated, and I did not change them.

### Item 1: application-tagged Double (tag 5): IMPLEMENTED
- `bac_enc_double()` writes `55 08` and then the 8 IEEE-754 binary64 octets,
  most significant octet first. This is the clause 20.2.7 encoding; 72.0
  encodes as `55 08 40 52 00 00 00 00 00 00`. It returns 10, or -1 when
  `buf` is NULL or `cap < 10`. On error it writes nothing (G1).
- `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is
  exactly 8. Any other length, or truncated input, gives -1. As for the other
  tags, a length in the non-minimal extended form (e.g. `55 fe 00 08 ...`)
  and an extended tag-number form (`f5 05 08 ...`) are accepted (D1b).
- A `_Static_assert(sizeof(double) == 8)` was added. The codec copies the
  bytes of the double into a `uint64_t` in the same way `REAL` uses a
  `uint32_t`.
- Test change: `tests/unit.vec` p0526 (D2) expected `55 08 00..00` to be
  rejected, because Double was unsupported. CR-101 changes that behaviour,
  so the vector now expects `OK n=10 d 0000000000000000` and has a comment
  saying why. p0528 and p0529 still expect ERR (truncated or wrong length).

### Item 2: context-tagged Enumerated and Signed: IMPLEMENTED
- `bac_enc_ctx_enumerated()` and `bac_enc_ctx_signed()` work like
  `bac_enc_ctx_unsigned()`. They use the same minimal-length content as the
  application forms (clauses 20.2.5 and 20.2.11), set the class bit to
  context, and use the extended tag-number form for tags 15 to 254 (G3).
  Tag 255 is reserved and rejected. They check the capacity before writing
  anything (G1).

### Tests
- `tests/unit.vec` has new blocks `c101-*` (tag `CR101`) covering Double
  encode and decode, context Enumerated and context Signed, with boundaries,
  extended tags, tag 255 and capacity limits.
- `acceptance.vec` has new blocks: `double-72` (the clause 20.2.7 example),
  `decode-double`, `context-enumerated` and `context-signed`.
- 363/363 vectors pass. They also pass in a separate ASan/UBSan build.

### Open points to confirm
- **NaN handling for Double.** The CR does not say what to do with NaN. I
  followed the existing REAL rule (E5a): the encoder rejects any NaN and
  returns -1, while +/-Infinity, subnormals and -0.0 are encoded. The decoder,
  like REAL (D5), accepts any bit pattern, NaN included. If the product owner
  wants NaN doubles encoded, the only change needed is to remove the `isnan`
  check in `bac_enc_double()`, plus the vectors c101-11..13.
- **Requirement IDs.** No requirements document is in this directory. The
  new vectors use provisional tags (EDBL, EDBLa, DDBL, ECENUM, ECSGN) until
  the requirements list gets official IDs for these functions. The D2
  requirement text, if it lists tag 5 as unsupported, should be updated to
  match p0526.
- **Platform.** The code assumes `double` is IEEE-754 binary64 with the same
  byte order as `uint64_t`. That holds on all current mainstream targets, but
  not on old mixed-endian ARM FPA ABIs.

## CR-102

Neither item conflicts with ASHRAE 135 clause 20.2 or with the requirements the
existing tests encode, so both are implemented. External behaviour is
unchanged. The public API (`bacapp.h`) is unchanged, the decoders are untouched,
and `driver.c` and `run_vectors.py` were not modified.

### Item 1: no floating-point arithmetic, comparisons or `<math.h>`: IMPLEMENTED
- `#include <math.h>` is gone. The only floating-point operations in
  `bacapp.c` were the two `isnan()` calls in `bac_enc_real()` and
  `bac_enc_double()`. Both functions now `memcpy` the value into a
  `uint32_t`/`uint64_t` first and then test the bits with integer operations:
  NaN means `(bits & 0x7FFFFFFF) > 0x7F800000` for binary32 and
  `(bits & 0x7FFFFFFFFFFFFFFF) > 0x7FF0000000000000` for binary64
  (`real_bits_nan()`, `double_bits_nan()`). The E5a/EDBLa behaviour is the same
  as before. Every NaN is rejected, whatever its sign or payload and whether it
  is quiet or signalling. +/-Infinity, subnormals and -0.0 are still encoded.
- `float`/`double` still appear in the API and in `bac_value_t`. They are only
  passed through and copied with `memcpy`, which needs no soft-float routine
  (under the soft-float ABI they travel in integer registers). Changing the API
  was out of scope because the CR requires external behaviour to stay the same.
- Added `_Static_assert(sizeof(float) == 4)` next to the existing Double check.
- `Makefile`: dropped `-lm`, since nothing needs libm any more.
- Check: I compiled `bacapp.c` with `clang --target=thumbv6m-none-eabi
  -mcpu=cortex-m0 -mfloat-abi=soft` at -Os and at -O2, using a stub
  `string.h` because no newlib is installed here. Before the change, the object
  referenced `__aeabi_fcmpun` and `__aeabi_dcmpun`, which came from `isnan`. Now
  its only undefined symbols are `memcpy` and `memset`. At -Os, the object's
  `.text` shrank from 2460 to 2316 bytes. I did not measure the linked image,
  so the ~2 KB soft-float saving is not confirmed on the real toolchain.

### Item 2: one tag-header writer and one minimal-length integer writer: IMPLEMENTED
- `put_tag()` is now the only code that builds a tag header (clause 20.2.1):
  - the class bit;
  - the extended tag-number form for tags 15 to 254, with reserved tag 255
    rejected here (G3; before, each context encoder checked this itself);
  - the L/V/T field, written directly or in the extended 1/3/5-octet length
    form (G4);
  - opening and closing tags (L/V/T 6 and 7, G5).

  Before writing anything it checks that the header plus the content fit in
  the buffer (G1). It replaces `hdr_len()` + `put_hdr()` (the same branching
  written twice) and `enc_open_close()`. It also replaces the header octets
  that were hand-written in Null, Boolean, REAL (`44`), Double (`55 08`), Date
  (`A4`), Time (`B4`) and Object Identifier (`C4`). Every encoder now uses it.
- `put_int()` is now the only minimal-length integer writer (clauses 20.2.4,
  20.2.5 and 20.2.11). One length rule covers both cases: zero-extension for
  Unsigned and Enumerated, sign-extension for Signed. It replaces
  `uint_len()`, `sint_len()`, `enc_uint()` and `enc_sint()`. All six
  Unsigned/Enumerated/Signed encoders use it, in application and in context
  form.
- How I read the item: every encoder that writes a minimal-length integer
  uses this one writer. Fixed-width contents do not go through it, because
  the standard does not allow them to be shortened:
  - REAL: 4 octets (20.2.6)
  - Double: 8 octets (20.2.7)
  - Date and Time: 4 octets each (20.2.12, 20.2.13)
  - Object Identifier: 4 octets (20.2.14)
  - the extended length field: fixed 1/2/4-octet forms (20.2.1.3.1)
  - the context Boolean octet (20.2.3)

  Those contents are written with the shared fixed-width helper `put_be()`.
  If "all encoders" was meant literally, so that these contents would also be
  shortened, that part conflicts with clause 20.2 and is not implemented. For
  example, object-id (3, 15) would lose its leading `00` octet.

### Tests
- `tests/unit.vec` has 45 new blocks, `c102-*` (tag `CR102`). They cover:
  - the edges of the bit-pattern NaN test: negative NaNs, maximum finite
    values, subnormals, -0.0, and Double NaNs whose fraction bits are only in
    the high or only in the low 32-bit word;
  - each encoder's path through `put_tag()`/`put_int()`: integer length
    boundaries, exact capacities, extended tags, open/close/ctx-unsigned with
    tag 255, and the 65535-octet extended-length boundary.

  Their expected outputs were produced by the pre-CR-102 build. They pass on
  both the old and the new build, so they document behaviour that did not
  change.
- 408/408 vectors pass. They also pass in a separate ASan/UBSan build.
- I also ran a differential test of the old build against the new one, and it
  found no differences:
  - about 105 M direct encoder calls, comparing the return value and a 64-byte
    output buffer. These covered context tags 0 to 255 with capacities 0 to 8
    and all 2^k±3 integer boundaries, 20 M random integers, a dense sample of
    binary32 bit patterns (every pattern around the Inf/NaN boundary), and
    5 M binary64 patterns;
  - 300 k random driver commands, decoders included.

### Open points to confirm
- The CR-101 note above says the NaN rule for Double can be changed by
  removing the `isnan` check in `bac_enc_double()`. That check is now the
  `double_bits_nan()` test.
- This CR only covers `bacapp.c`. Firmware code that calls
  `bac_enc_real()`/`bac_enc_double()` or reads `v.r`/`v.d` still links the
  soft-float routines if it does its own float arithmetic.
- The bit-pattern approach keeps the assumption recorded for CR-101: `float` and
  `double` are IEEE-754 and have the same byte order as `uint32_t`/`uint64_t`.

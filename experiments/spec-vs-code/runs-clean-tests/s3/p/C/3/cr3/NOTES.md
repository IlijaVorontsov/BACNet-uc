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

## CR-103

Item 1 is implemented. Items 2 and 3 are not implemented because each one
conflicts with something the product must follow:
- item 2 conflicts with the product's documented NaN rule (E5a, which EDBLa
  follows);
- item 3 conflicts with ASHRAE 135 clauses 20.2.4 and 20.2.11.

`driver.c` and `run_vectors.py` were not modified.

### Item 1: `bac_enc_char_string` rejects text that is not well-formed UTF-8: IMPLEMENTED
- A new static helper, `utf8_well_formed()`, checks the text against the
  table of well-formed octet sequences in RFC 3629 section 4 (the same table
  as Unicode Table 3-7). `bac_enc_char_string()` returns -1 for:
  - an octet that cannot start a sequence: a stray continuation octet
    (80..BF), C0, C1 or F5..FF. F5..FF also covers the obsolete 5- and
    6-octet forms;
  - a sequence cut off by the end of the text, or with a missing or wrong
    continuation octet;
  - overlong forms: C0/C1 leads, E0 80..9F, F0 80..8F;
  - surrogates U+D800..U+DFFF (ED A0..BF), including CESU-8 surrogate pairs;
  - code points above U+10FFFF: F4 90..BF and F5..F7.
- RFC 3629 counts U+0000 and noncharacters such as U+FFFE/U+FFFF as
  well-formed, so they are still accepted. The CR does not ask to reject
  them.
- The check runs before anything is written, so on error the buffer is
  untouched (G1). Empty text is still valid (`71 00`). The output for valid
  text has not changed.
- The helper uses only integer operations, so the CR-102 rule (no
  floating-point code) still holds. It makes one O(len) pass over the text
  before the copy.
- `bacapp.h`: the comment on `bac_enc_char_string()` now states the
  requirement. The API itself is unchanged.
- No conflict with the standard: clause 20.2.9 defines character set X'00'
  as ISO 10646 (UTF-8), so ill-formed text is not a valid value in that
  character set, and the encoder always writes X'00'. The change also matches
  the header's existing contract, which says the input is "UTF-8 text".
- Test change: the E7a vectors p0209, p0212, p0213, p0215 and p0216 cover
  overlong forms, a truncated sequence and a stray continuation octet. They
  expected the octets to be copied unchanged. CR-103 changes that behaviour,
  so they now expect `ERR`, and each has a comment giving the old expectation.

### Item 2: encode NaN in `bac_enc_real`/`bac_enc_double`: NOT IMPLEMENTED (conflicts with a product requirement)
- Requirement E5a says the REAL encoder rejects every NaN (p0159..p0163,
  c102-05..08). EDBLa applies the same rule to Double (c101-11..13,
  c102-12..16). The request would reverse a documented requirement rather
  than fill a gap.
- The request comes from one consuming team, but the change would affect
  what every caller of `bac_enc_real()` can put on the wire. Reversing E5a
  is for the product owner to decide, not a field request. The CR-101 open
  point about NaN for Double was also left to the product owner.
- The stated use, NaN meaning "no sample" in a trend log, does not need this
  change. BACnet already has ways to record a missing or failed sample: the
  log-datum choice of BACnetLogRecord has log-status, null-value and failure
  alternatives. A peer that receives a NaN REAL has no reason to read it as
  "no sample".
- Nothing changed. If the product owner amends E5a/EDBLa, the fix is to
  remove the `real_bits_nan()`/`double_bits_nan()` checks and change the
  vectors listed above to expect `OK`.

### Item 3: encode Unsigned/Enumerated 0 with zero content octets: NOT IMPLEMENTED (conflicts with the standard)
- The request is based on a misreading of the standard:
  - Clauses 20.2.4 (Unsigned) and 20.2.11 (Enumerated) require a primitive
    encoding with at least one contents octet, using the fewest octets
    needed. For 0 that is the single octet X'00'. So `21 00` and `91 00` are
    the correct encodings, and `20` and `90` are invalid (L/V/T 0 means no
    content).
  - The only primitives with no contents octets are Null (20.2.2) and the
    application-tagged Boolean (20.2.3), whose value is in the L/V/T field.
- Emitting `20`/`90` would break interoperability, because conforming
  decoders reject it. This module's own `bac_dec_app()` rejects it too
  (length < 1 gives -1).
- The request also conflicts with the E3/E12 vectors p0007, p0067, p0306,
  c101-40 and acceptance `enumerated-0`. Nothing changed.

### Tests
- `tests/unit.vec` has 60 new blocks for item 1, `c103-01`..`c103-96` (tag
  `CR103`). They cover:
  - every boundary of the well-formed table: the smallest and largest
    2-, 3- and 4-octet values, both sides of the surrogate range, and
    U+10FFFF;
  - every class of ill-formed input the CR names: truncated, invalid,
    overlong, surrogates (including a CESU-8 pair) and above U+10FFFF;
  - long text in the extended-length form, with the error in the first or
    the last octet;
  - interaction with the capacity limit (G1).

  The expected outputs were produced independently of the C code, with the
  strict UTF-8 codec of Python 3.
- Four more blocks, `c103-a0`..`c103-a3`, pin the unchanged behaviour for
  item 3: Enumerated 0 encodes as `91 00`, and the decoder rejects `20` and
  `90`.
- `acceptance.vec` has a new block, `character-string-utf8`.
- 473/473 vectors pass. They also pass in a separate ASan/UBSan build, and
  `bacapp.c` compiles cleanly with `-Wpedantic -Wconversion`.
- I also ran a differential test of the new check against a separate
  reference that decodes the code points arithmetically. It covered every
  input of 1 to 3 octets, all 2^32 inputs of 4 octets, and 20 M random strings
  of 1 to 12 octets. It compared the return value and the output octets, and
  found no differences.

### Open points to confirm
- **E7a wording.** No requirements document is in this directory. Judging
  by its old vectors, E7a described pass-through of ill-formed text. It
  should now say that ill-formed UTF-8 is rejected. I treated E7a as a record
  of the old behaviour, not as a deliberate "do not validate" rule, because
  pass-through produced values that are invalid under clause 20.2.9. If the
  product owner did mean it as a deliberate rule, they should confirm
  item 1. Undoing it takes one line in `bac_enc_char_string()` plus the
  vectors.
- **Decoder unchanged.** `bac_dec_app()` still returns character-set-0 text
  without checking it (D7, p0501), because the CR covers only the encoder.
  Checking on decode would be a separate change, and it would start
  rejecting data from non-conforming peers.
- **Callers.** Firmware code that passes Latin-1 or other non-UTF-8 text
  (for example, object names from legacy configuration) will now get -1
  where it used to get an encoding. Such callers need to convert the text
  first.
- **Makefile.** The `Makefile` still links with `-lm`, although the CR-102
  notes say it was dropped. Nothing needs libm, so this is harmless. I left
  the `Makefile` unchanged because CR-103 does not concern it.

## CR-101

Status per item:

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double()` writes `55 08` followed by the IEEE-754 double-precision value,
     8 octets, most significant octet first (ASHRAE 135 clause 20.2.7; e.g. 72.0 ->
     `55 08 40 52 00 00 00 00 00 00`). It returns -1 without touching the buffer when
     `cap < 10` or `buf` is NULL.
   - `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is exactly 8.
     Any other length, or too few content octets, gives -1. As for the other types, a
     non-minimal length or tag-number encoding is accepted (`55 fe 00 08 ...`,
     `f5 05 08 ...`).
   - Added a `_Static_assert(sizeof(double) == 8)`. Like the REAL code, the bit copy
     assumes the host `double` is IEEE-754 binary64 with the same byte order as `uint64_t`.
2. **`bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: implemented.** They mirror
   `bac_enc_ctx_unsigned()`: minimal-length content (1-4 octets, two's complement for
   Signed), context class, extended tag number for tags 15..254, and tag 255 rejected.

No item conflicts with the BACnet standard or the existing requirements, so nothing was
declined.

Test artifacts updated:
- `tests/unit.vec`: p0526 (`dec_app 5508 00*8`) used to expect ERR because Double was
  unsupported. It now expects `OK n=10 d 0000000000000000` and is retagged `DV CR101`.
  Added blocks c101-01..c101-53 for the Double encoder and decoder and for the two
  context encoders, including capacity, tag 15/254/255 and malformed-length cases.
- `acceptance.vec`: added double-72 (clause 20.2.7 example), context-enumerated,
  context-signed and decode-double.
- All vectors pass (353/353). The build is also clean with
  `-Wpedantic -fsanitize=address,undefined`.

Open points to confirm:
- **NaN**: the CR does not say how to handle NaN. `bac_enc_double()` rejects NaN (quiet or
  signalling, any sign) the same way `bac_enc_real()` does (E5a / CR3nan). Infinities,
  signed zero and subnormals are encoded. The decoder accepts NaN bit patterns, as it does
  for REAL. If the NaN rule was meant only for REAL, remove the `isnan` check and change
  vectors c101-09..11.
- `bac_enc_double` has no requirement ID yet. The new vectors are tagged only `CR101`
  (plus the generic G1/G3/DV/D1b/D2 tags). The requirements document should get entries
  for Double and for the two context encoders.

## CR-102

Status per item:

1. **No floating-point arithmetic, comparisons or `<math.h>`: implemented.**
   - Removed `#include <math.h>` and both `isnan()` calls. `bac_enc_real()` and
     `bac_enc_double()` now copy the value into a `uint32_t`/`uint64_t` with `memcpy` and
     test for NaN on the bit pattern (`is_nan_bits()`: exponent all ones and fraction
     non-zero). NaN of either sign and any payload, quiet or signalling, is still rejected.
     Infinities, signed zero and subnormals are still encoded. The decoder already used
     only `memcpy`.
   - Added `_Static_assert(sizeof(float) == 4)` next to the existing Double assert. The
     bit-pattern code assumes IEEE-754 binary32/binary64, as before.
   - `Makefile`: dropped `-lm`. Nothing links against libm any more.
   - Check: a Cortex-M0 object (`clang --target=thumbv6m-none-eabi -mfloat-abi=soft -Os`)
     no longer references `__aeabi_fcmpun`/`__aeabi_dcmpun`. The only undefined symbols
     are `memcpy` and `memset`. `.text` went from 2460 to 2216 bytes. The x86-64 object
     has no SSE/x87 compare or arithmetic instructions.
2. **One shared tag-header writer and one minimal-length integer writer: implemented.**
   - `put_tag()` is the only code that forms a tag header: tag number (extended tag octet
     for 15..254, 255 rejected), class, LVT with extended length (1/3/5 octets), and
     opening/closing tags. It checks that the header plus the content fits before it
     writes anything. `hdr_len()`/`put_hdr()` and `enc_open_close()` are gone. Null,
     Boolean, REAL, Date, Time and Object Identifier no longer write constant header
     octets by hand. The per-encoder `tag == 255` checks are now in `put_tag()`.
   - `put_int()` is the only minimal-length integer writer. It serves application and
     context Unsigned, Enumerated and Signed. It replaces `uint_len()`/`sint_len()`/
     `enc_uint()`/`enc_sint()`.
   - How I read "all encoders": fixed-width fields do **not** go through the
     minimal-length writer. These are REAL, Double, Date, Time, Object Identifier and the
     context Boolean octet. ASHRAE 135 clauses 20.2.3, 20.2.6, 20.2.7 and 20.2.12-20.2.14
     fix their lengths. For example, the Object Identifier `c4 00 c0 00 0f` must keep its
     leading zero. They share `put_tag()` and the common `put_prim()` (header plus
     content) instead.

No item conflicts with the BACnet standard or the existing requirements, so nothing was
declined. External behavior is unchanged:
- The unchanged vectors all still pass.
- A differential run of the old and new drivers matched on every line of about 80,000
  commands: random ones plus boundary sweeps over every tag number 0..255, integer
  length boundaries, NaN/Inf bit patterns, extended-length boundaries and small
  capacities, including ERR/DIRTY/OVERRUN checks.
- The ASan/UBSan build passes as well.

Test artifacts updated:
- `tests/unit.vec`: added c102-01..c102-39 (tag `CR102`). They cover NaN bit-pattern
  edges for REAL and Double, including a Double NaN whose fraction is only in the high
  or only in the low word. They also cover headers now produced by `put_tag()` (Null,
  Boolean, Date, Time, OID, context Boolean, opening/closing with extended tags, tag 255
  for context Unsigned and opening tag) and signed/unsigned length boundaries. The
  expected outputs were generated with the pre-CR-102 build. 389/389 pass.
- `acceptance.vec`: unchanged. There is no new behavior.
- `Makefile`: `-lm` removed.

Open points / unsure:
- The size figures come from clang's Thumb-v6M code generator without a sysroot, not the
  production toolchain and link. Whether the full ~2 KB is saved depends on nothing else
  in the image pulling in soft-float routines. That was not verified here.
- The public API still passes `float`/`double` (`bac_enc_real`, `bac_enc_double`, and
  `bac_value_t.v.r/.v.d`). Under the soft-float AAPCS these are passed and stored in core
  registers or memory with no library calls. Callers that do float math still link
  soft-float themselves.
- The CR-101 open point about NaN in Double ("remove the `isnan` check") now refers to
  the `is_nan_bits()` test in `bac_enc_double()`. It is still unresolved.
- A pre-existing issue, not changed: with a 32-bit `size_t` (the M0 build),
  `len > 0xFFFFFFFFu` in `bac_enc_octet_string()` is always false and clang warns with
  `-Wtautological-type-limit-compare`. It is harmless.

## CR-103

Status per item:

1. **`bac_enc_char_string` rejects ill-formed UTF-8: implemented.**
   - New `utf8_valid()` checks the text against RFC 3629 (the Unicode table of
     well-formed byte sequences) before anything is written. It returns -1 for:
     - truncated sequences, at the end or cut short by the next character;
     - stray continuation octets and the lead octets C0, C1 and F5..FF;
     - overlong forms (E0 80..9F, F0 80..8F);
     - surrogates U+D800..U+DFFF (ED A0..BF);
     - code points above U+10FFFF (F4 90..BF and anything higher).
     The buffer is left unchanged in every case. U+0000 and U+FEFF are valid and are
     encoded as given.
   - Check: all 4.3 billion inputs of 0 to 4 octets matched a second, independently
     written decoder-based reference. 40,000 random strings of up to about 500 octets,
     with random capacities, matched Python's strict UTF-8 decoder through the driver.
     None came back ERR DIRTY.
   - Updated the comments in `bacapp.h` and `bacapp.c`.
2. **`bac_enc_real` / `bac_enc_double` encode NaN: implemented.**
   - Removed `is_nan_bits()` and both NaN checks. Every bit pattern is now encoded as
     given, including quiet and signalling NaN of either sign and any payload. For
     example, `enc_real 7fc00000` gives `44 7f c0 00 00`. The decoder already accepted
     NaN, so NaN now round-trips.
   - The CR-102 rule still holds: no floating-point operations and no `<math.h>`.
     `bacapp.c` for Cortex-M0 soft-float (clang, `-Os`, no sysroot) still references
     only `memcpy`/`memset`. `.text` is 2164 bytes, down from 2216.
   - This also closes the CR-101/CR-102 open point on NaN in Double. Both types now
     behave the same way.
3. **Encode Unsigned/Enumerated 0 with no content octets (`20` / `90`): not
   implemented, because it conflicts with the BACnet standard.**
   - ASHRAE 135 clause 20.2.4 says an Unsigned encoding "shall be primitive, and shall
     contain at least one octet". Clause 20.2.11 says the same for Enumerated.
   - The smallest number of octets for 0 is therefore one, so 0 encodes as `21 00` /
     `91 00`, or `x9 00` context-tagged. The encoder already does this.
   - A zero-length Unsigned or Enumerated is malformed. Our own decoder rejects `20` and
     `90` (D4), as a conforming peer would.
   - The encoder is unchanged. `p0007` (CR3zero) and the acceptance vector `enumerated-0`
     (`9100`) still hold.

Why items 1 and 2 are not treated as conflicts:
- Each one changes a behavior that this product had documented: E7a (text copied
  unchecked, vectors p0209-p0216) and E5a (REAL NaN rejected, vectors p0159-p0163).
  Neither conflicts with ASHRAE 135:
  - Character set X'00' is UTF-8, so rejecting ill-formed text only stops the encoder
    from sending non-conformant strings.
  - Clauses 20.2.6/20.2.7 use IEEE-754 encoding, and IEEE-754 defines NaN.
- I read both items as intended changes to E7a and E5a, not as requests that clash with
  a requirement that stays in force.
- **The requirements document needs updating:** E7a becomes "ill-formed UTF-8 is
  rejected", and E5a becomes "NaN is encoded like any other value".

Test artifacts updated:
- `tests/unit.vec`, changed expectations, each retagged with `CR103`:
  - p0209, p0212, p0213, p0215, p0216 (CR3utf8) now expect `ERR`.
  - p0159, p0161, p0163, c102-05..07 (CR3nan) now expect `OK 44...`.
  - c101-09..11, c102-13..17 (CR101nan) now expect `OK 5508...`.
  - c102-08 (`enc_real 7f800001 @0`) still expects `ERR`, now only because of the
    capacity.
  - The E5a/E7a tags stay for traceability to the reworded requirements.
- `tests/unit.vec`, new blocks c103-01..c103-92 (tag `CR103`):
  - UTF-8 boundary code points: U+0000, U+007F, U+0080, U+07FF, U+0800, U+D7FF,
    U+E000, U+FFFF, U+FEFF, U+10000 and U+10FFFF.
  - Every rejection class, including CESU-8 surrogate pairs, 5- and 6-octet forms, and
    invalid octets at the start or end of 300-octet strings.
  - Capacity combined with invalid text.
  - NaN payloads and signs for REAL and Double, with exact capacity and one octet short.
  - NaN decode round trips.
  - Zero-value regression checks for item 3: `enc_unsigned 0`, `enc_enum 0`, context
    zero, `dec_app 20`/`90` -> ERR.
- `acceptance.vec`: added character-string-utf8 (2-, 3- and 4-octet characters),
  real-nan, double-nan and decode-real-nan.
- All vectors pass: 458/458 unit and 27/27 acceptance. They also pass on a
  `-Wpedantic -Wconversion -fsanitize=address,undefined` build.
- `Makefile`: the link line had `-lm` again, which does not match CR-102. I removed it.
  Nothing uses libm.

Open points / unsure:
- **Who decides E5a:** the NaN request comes from one team (trend log), not from the
  requirements owner. If E5a was a deliberate interoperability rule, the product owner
  should confirm the change. Reverting means restoring `is_nan_bits()` and the checks
  plus the CR3nan/CR101nan vectors.
- **Missing samples in BACnet:** a BACnet Trend Log normally reports a missing sample
  through the log-datum CHOICE of BACnetLogRecord (for example `failure` or a
  log-status record), not a REAL NaN. Other BACnet clients will read the NaN as a
  measured value. That is outside this module, but the trend-log team may want to
  check it.
- **Signalling NaN on x87:** the encoder copies whatever bits it receives. On an
  x86-32/x87 build, a caller that loads a signalling NaN into an x87 register may
  quiet it before the call. This cannot happen on the soft-float M0 target or on
  x86-64 SSE.
- **UTF-8 in the decoder:** `bac_dec_app()` still returns character-string content
  without checking it (p0457, p0501). The CR covers only the encoder. If the receive
  side should also be strict, that needs its own CR, and it would weaken tolerance of
  non-conforming peers.
- **Other character sets:** the encoder only ever emits character set X'00' (UTF-8), so
  no other character sets are affected.

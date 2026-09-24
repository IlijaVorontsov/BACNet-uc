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

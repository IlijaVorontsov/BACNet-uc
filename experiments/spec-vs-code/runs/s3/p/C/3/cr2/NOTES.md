# NOTES

## CR-101

Neither item conflicts with the BACnet standard (ASHRAE 135 clause 20.2) or with
the product's existing behavior, so both are implemented. `bacapp.h` and `driver.c`
were already updated and were not modified.

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double()` writes `55 08` followed by the IEEE-754 double in 8 octets,
     big-endian, and returns 10. Content length 8 does not fit in the LVT field, so
     the header uses LVT=5 plus an extended-length octet. This matches the standard
     (clause 20.2.7, e.g. 72.0 -> `55 08 40 52 00 00 00 00 00 00`). If `cap < 10` it
     returns -1 and leaves the buffer untouched.
   - `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is exactly
     8. Any other length is rejected. Like every other tag, the length can be given
     in any valid form (for example `55 FE 00 08 ...`), and trailing bytes are ignored.
     The decoder accepts NaN payloads, just as the REAL decoder does.
   - Tests: `tests/unit.vec` block `p0526` expected `ERR` for `dec_app 5508 00...00`
     because Double was not supported before. It now expects
     `OK n=10 d 0000000000000000`. I also removed its `D2` tag: that tag marks decode
     rejection cases, and this block is no longer one. New blocks `cr101-e*` and
     `cr101-d*` were added, and `acceptance.vec` got `double-72` and `decode-double`.
2. **`bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: implemented.**
   They work like `bac_enc_ctx_unsigned()`: minimal-length contents (unsigned for
   Enumerated, two's complement for Signed), context class, extended tag number for
   tags >= 15, and -1 for tag 255 (reserved) or when capacity is too small. New blocks
   `cr101-c*` and `cr101-s*` were added, and `acceptance.vec` got `context-enumerated`
   and `context-signed`.

Open questions:
- **NaN in `bac_enc_double()`.** The CR says nothing about NaN. `bac_enc_real()`
  rejects NaN (the CR-3 `CR3nan` vectors), so `bac_enc_double()` also returns -1 for
  any NaN, quiet or signaling. ±Inf, ±0 and subnormals are encoded. The product owner
  should confirm this. The tests for it are tagged `CR101nan`.
- The `E*`/`D*` tags in `tests/unit.vec` appear to be requirement IDs from a document
  that is not in this directory. I did not give the new vectors requirement IDs. They
  are tagged `CR101`, plus `G1` (capacity) or `G3` (extended tag number) where those
  clearly apply. Requirement IDs for Double and the new context encoders should be
  added once the requirements document is updated.

## CR-102

Neither item conflicts with the BACnet standard (ASHRAE 135 clause 20.2) or with
the documented behavior, so both are implemented. External behavior is unchanged:
`bacapp.h` was not modified, and every encoder and decoder gives the same results
and the same return values as before, including -1 with the buffer left untouched.
`driver.c` and `run_vectors.py` were not modified.

1. **No floating point / `<math.h>`: implemented.**
   - `#include <math.h>` and both `isnan()` calls are gone. `bac_enc_real()` and
     `bac_enc_double()` copy the value's bit pattern with `memcpy` and test for NaN
     with integer operations (`real_bits_nan()`, `double_bits_nan()`): the exponent is
     all ones and the fraction is non-zero. Quiet and signaling NaNs of either sign
     are still rejected, and ±Inf, ±0 and subnormals are still encoded, so the
     CR-3 / CR-101 NaN rules still hold. The decoders already used only `memcpy`.
   - `_Static_assert`s check that `float` is 4 bytes and `double` is 8 bytes.
   - `Makefile`: removed `-lm`, because nothing links against libm anymore.
   - Checked with a clang cross-compile for `thumbv6m-none-eabi -mcpu=cortex-m0
     -mfloat-abi=soft` at -O0/-O1/-O2/-Os. The only undefined symbols left are
     `memcpy` and `memset`. Before, the object also needed `__aeabi_fcmpun` and
     `__aeabi_dcmpun`. The x86 assembly no longer has `ucomiss`/`ucomisd` or any
     other SSE arithmetic or compare instruction.
2. **One tag-header writer and one minimal-length integer writer: implemented.**
   - `put_tag()` is now the only code that writes a tag header: the initial octet,
     the extended tag number for tags >= 15, the extended length (1, 3 or 5 octets)
     for LVT >= 5, and the opening/closing tags (LVT 6/7). Called with `buf == NULL`
     it only computes the header size, so the separate `hdr_len()`/`put_hdr()` pair,
     which duplicated this logic, is gone. It also rejects the reserved tag number
     255. `begin()` checks the capacity and then calls `put_tag()`. Every encoder now
     goes through it: Null, Boolean, REAL, Double, Date, Time, Object Identifier and
     the opening/closing tags used to hard-code their header bytes, and
     `enc_open_close()` had its own copy of the header logic.
   - `put_int()` is now the only minimal-length integer writer. `uint_len()`,
     `sint_len()`, `enc_uint()` and `enc_sint()` were removed. A flag picks unsigned
     or two's-complement minimisation. It has to handle both, because the minimal
     encodings differ (clause 20.2.4 and 20.2.5): 128 is `80` as Unsigned but
     `00 80` as Signed. Using the unsigned rule for Signed would break the standard.
     `enc_int()` (header + `put_int()`) is used by all Unsigned, Enumerated and
     Signed encoders, both application and context.
   - How I read "all encoders": REAL, Double, Date, Time and Object Identifier have
     fixed 4- or 8-octet contents under the standard, and the Boolean contents are
     not integers. So these encoders use the shared header writer but not the
     minimal-length writer. Sending them through `put_int()` would change the wire
     format and break clause 20.2.
   - Size on Cortex-M0 (`.text` of `bacapp.o`, clang): 4016 -> 3912 bytes at -O2 and
     2460 -> 2440 bytes at -Os. This does not include the soft-float routines that
     are no longer linked.

Verification:
- `make` builds with no warnings (also clean with `-Wpedantic -Wconversion
  -Wsign-conversion`). All vectors pass: `tests/unit.vec` + `acceptance.vec`,
  377/377. They also pass under ASan/UBSan.
- A differential run compared the pre-change build and the new build on 148,414
  driver commands, with the same output for every one. The commands covered every
  encoder, capacities 0..14, every context tag 0..255, integer length boundaries,
  NaN/Inf/subnormal bit patterns for REAL and Double, extended-length boundaries
  up to 65537 octets, and random `dec_app`/`dec_tag` inputs.
- New regression blocks `cr102-*` (27) in `tests/unit.vec` cover the NaN bit-pattern
  boundaries, the header-writer extended-length/tag boundaries and the integer-writer
  length boundaries. Their expected values were produced by the pre-change build.
  The blocks are tagged `CR102`, plus the requirement tags that existing blocks use
  for the same encoder (E*/G*/CR3nan/CR101nan). `acceptance.vec` did not need
  changes.

Open questions:
- No `arm-none-eabi-gcc`/libgcc was available here. So the absence of soft-float
  routines was checked from the undefined symbols of a clang Cortex-M0 object, not
  from a real firmware link map, and the ~2 KB flash saving was not measured. Please
  check the link map of the real M0 build.
- The public API still passes `float`/`double` by value (`bacapp.h` was not changed,
  since that would be an external change). Under the soft-float AAPCS these go in
  core registers and need no support routines. Callers that do their own float math
  will still pull in soft-float code, but that is outside `bacapp.c`.
- NaN detection now depends on the IEEE-754 binary32/binary64 layout (and on
  float/integer byte order being the same). The encoded output already depended on
  that, and it holds on Cortex-M and x86.

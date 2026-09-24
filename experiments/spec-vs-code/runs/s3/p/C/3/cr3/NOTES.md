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

## CR-103

Item 1 is implemented. Items 2 and 3 are **not** implemented because they
conflict: item 2 with a documented product requirement, item 3 with the BACnet
standard. `driver.c`, `run_vectors.py`, `Makefile` and `acceptance.vec` were not
modified.

1. **Reject ill-formed UTF-8 in `bac_enc_char_string()`: implemented.**
   - A new helper, `utf8_well_formed()`, checks the text against RFC 3629 (Unicode
     Table 3-7 well-formed byte sequences) before anything is written.
     `bac_enc_char_string()` returns -1 and leaves the buffer untouched for any of
     these: a stray continuation octet, the lead octets C0/C1/F5..FF (this covers the
     old 5- and 6-octet forms), a truncated sequence (also at the end of the text),
     a lead octet followed by a non-continuation octet, overlong encodings
     (C0/C1, E0 80..9F, F0 80..8F), surrogates U+D800..U+DFFF (ED A0..BF, so
     CESU-8 pairs are rejected as well), and code points above U+10FFFF (F4 90..BF
     and above). Well-formed text is copied byte for byte, as before. U+0000 is
     well-formed UTF-8 and is still accepted.
   - Validation runs before the capacity check, so ill-formed text gives -1 even
     when the old unvalidated encoding would have fit.
   - `bacapp.h`: the comment on `bac_enc_char_string()` now documents the -1 for
     ill-formed text. The prototype is unchanged.
   - Tests: `tests/unit.vec` blocks `p0209`, `p0212`, `p0213`, `p0215` and `p0216`
     (tags `E7a CR3utf8`: C0 AF, E2 82, 80, C1 BF, E0 80 AF) expected the bytes to be
     passed through (`OK 73...`). They now expect `ERR` and are also tagged `CR103`.
     New blocks: `cr103-v*` (well-formed boundaries U+0000, U+007F, U+0080, U+07FF,
     U+0800, U+D7FF, U+E000, U+FFFF, U+10000, U+10FFFF, mixed text), `cr103-x*`
     (surrogates, > U+10FFFF, F5..FF, 5/6-octet forms, overlongs, truncated and
     invalid sequences, one bad octet inside a 601-octet string) and `cr103-g*`
     (capacity, with the buffer left untouched).
   - Checked against Python's strict UTF-8 decoder on 227,168 inputs (every 1- and
     2-octet string, all 3-octet and 4-octet strings built from sampled lead and
     continuation octets, and 60,000 random mixed strings). There were no
     mismatches, also in an ASan/UBSan build. A differential run against the
     pre-change build showed that only `enc_str` with ill-formed input changes
     (including string lengths and capacities around the extended-length
     boundaries). Builds clean with `-Wpedantic -Wconversion -Wsign-conversion`.
   - The decoder was not changed. `bac_dec_app()` still returns the character-string
     bytes without validating them (for example `p0457` and `p0501`). The CR only
     names the encoder, and a received string may use a character set other than
     UTF-8.

2. **Encode NaN in `bac_enc_real()` / `bac_enc_double()`: not implemented
   (conflicts with a documented requirement).**
   The product requirement is that the REAL encoder rejects NaN. That rule is
   requirement E5a, the `CR3nan` vectors (`p0159`, `p0161`, `p0163`,
   `cr102-r05..r07`), the `bac_enc_real()` comment and the CR-3 decision recorded in
   CR-101 and CR-102 above. `bac_enc_double()` follows the same rule for
   consistency (CR-101, `CR101nan` vectors). A request from one consuming team
   cannot override a product requirement. The product owner has to change E5a
   first. The CR-101 open question on Double NaN is also still open.
   Also, using NaN to mean "no sample" is not how BACnet represents a missing trend
   sample. A BACnetLogRecord has explicit choices for that (`log-status`,
   `null-value`, `failure`). I recommend the trend-log team use those. Behavior is
   unchanged: every NaN (quiet or signaling, either sign) returns -1 from both
   encoders.

3. **Encode Unsigned/Enumerated 0 with zero content octets (`20` / `90`): not
   implemented (conflicts with the BACnet standard).**
   The request's premise is wrong. ASHRAE 135 clause 20.2.4 (Unsigned) and clause
   20.2.11 (Enumerated) say the encoding "shall contain at least one octet", so 0
   is `21 00` / `91 00`. That is what the encoder already emits (`p0007`,
   `acceptance.vec` `enumerated-0`). A zero-length value would be rejected by
   conforming peers, including our own `bac_dec_app()`, which rejects L = 0 for
   these tags. New blocks `cr103-z01..z04` pin this: `enc_enum 0` -> `9100`,
   `enc_ctx_enum 5 0` -> `5900`, and `dec_app 20` / `dec_app 90` -> `ERR`.

Open questions:
- The requirements document behind the `E*`/`D*` tags is not in this directory.
  For item 2 I used the E5a vectors and the recorded CR-3 decision. For item 1 the
  old `E7a` vectors expected ill-formed UTF-8 to be passed through. I took them as
  recording the old, unvalidated behavior, not a requirement to pass ill-formed text
  through, because emitting such bytes under character set 0 (UTF-8) is not valid
  UTF-8. If E7a actually requires pass-through, the requirements document and item 1
  need to be reconciled by the product owner.
- Any caller that used to send arbitrary bytes (for example Latin-1 text) through
  `bac_enc_char_string()` now gets -1 and has to convert to UTF-8 first.
- CR-102 says `-lm` was removed from the `Makefile`, but the current `Makefile`
  still links with `-lm`. The build does not need it. I left the `Makefile` as it
  is, because it may be the harness's copy.

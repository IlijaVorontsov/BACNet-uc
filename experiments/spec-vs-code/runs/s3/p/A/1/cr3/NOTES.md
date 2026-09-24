## CR-101

1. Application-tagged Double (tag 5): **implemented**.
   - `bac_enc_double()` writes `55 08` followed by the 8 octets of the IEEE-754
     double, most significant octet first (ASHRAE 135 clause 20.2.7; the
     standard's example 72.0 -> `55 08 40 52 00 00 00 00 00 00` is now in
     `acceptance.vec`). It needs 10 bytes of capacity and writes nothing on error.
   - `bac_dec_app()` now accepts tag 5 and requires a content length of exactly 8;
     the value is stored in `v.d`. Any other length is rejected. As with the other
     types, a non-minimal extended length (e.g. `55 fe 00 08 ...`) is accepted.
   - Uncertain: the CR does not say how NaN should be handled. For consistency with
     the existing `bac_enc_real()`, `bac_enc_double()` rejects NaN (returns -1).
     Infinities and negative zero are encoded. The decoder, like the REAL decoder,
     does not check the bit pattern, so a received NaN is still decoded.
2. `bac_enc_ctx_enumerated()` and `bac_enc_ctx_signed()`: **implemented**.
   They mirror `bac_enc_ctx_unsigned()`: the same minimal-length content
   encoding as the application-tagged Enumerated and Signed encoders, context
   class, extended tag numbers for tags 15-254, and tag 255 (reserved) is rejected.

Neither item conflicts with the BACnet standard, so both were implemented.
`bacapp.h` and `driver.c` were already updated and were not changed.
New vectors were added to `acceptance.vec` (double-72, decode-double,
context-enumerated, context-signed). All 23 acceptance blocks pass.

## CR-102

Neither item conflicts with the BACnet standard or the documented behavior, so
both were implemented. External behavior is unchanged.

1. No floating-point arithmetic, floating-point comparisons or `<math.h>`: **implemented**.
   - `#include <math.h>` and both `isnan()` calls are gone. `bac_enc_real()` and
     `bac_enc_double()` copy the value into a `uint32_t`/`uint64_t` with `memcpy`
     and reject NaN by its bit pattern (exponent all ones and a non-zero fraction:
     `(bits & 0x7FFFFFFF) > 0x7F800000`, and the same test on 64 bits for Double).
     As before, NaN is rejected (quiet, signalling, either sign), while infinities
     and negative zero are encoded. The decoder already used only `memcpy`.
   - `-lm` was removed from the `Makefile` because nothing needs libm now.
   - Two `_Static_assert`s require a 4-byte `float` and an 8-byte `double`. The
     `memcpy` calls depend on those sizes, and a toolchain with a 32-bit `double`
     now fails at compile time instead of reading past the variable.
   - Checked by compiling `bacapp.c` for `thumbv6m-none-eabi` (Cortex-M0,
     `-mfloat-abi=soft`) with clang. The old file called `__aeabi_fcmpun` and
     `__aeabi_dcmpun` from `isnan`. The new file calls no `__aeabi_f*`/`__aeabi_d*`
     routine, only `memcpy`/`memset`. The module's own `.text` at `-Os` went from
     2460 to 2304 bytes. At `-O2` it grew slightly (4016 -> 4044) because the
     shared helper is inlined.
2. One shared tag-header writer and one minimal-length integer writer: **implemented**.
   - `build_hdr()` is now the only code that builds a tag header: tag number
     (extended tag number octet for 15-254), class, and length (0-4 inline, or the
     extended length forms 5-253 / 254+2 octets / 255+4 octets). `begin()` wraps
     it. It checks capacity for the header plus contents, rejects a NULL buffer and
     the reserved tag number 255, and writes the header only when everything fits,
     so a failed call still leaves the buffer unchanged. Every encoder now goes
     through `begin()`. This includes Null, Boolean (the value is written in the
     LVT field), REAL, Double, Date, Time, Object Identifier, and the opening and
     closing tags (LVT 6/7). None of them hard-codes header octets any more. The
     separate `hdr_len()` and the header code inside `enc_open_close()` were
     removed, and so were the four per-encoder `tag == 255` checks.
   - `enc_int()` is the only minimal-length integer writer. It replaces
     `uint_len`/`sint_len`/`enc_uint`/`enc_sint` and serves Unsigned, Enumerated and
     Signed, both application and context tagged. Signed values keep their sign bit,
     so -128 is 1 octet, -129 is 2 octets and 128 is 2 octets.
   - Interpretation: "all encoders share one minimal-length integer writer" applies
     to the encoders whose contents the standard requires to be minimal length
     (Unsigned, Enumerated, Signed). REAL, Double, Object Identifier, Date and Time
     have fixed-length contents, and the extended length field uses fixed 1/2/4-octet
     forms. Encoding those at minimal length would break the standard (e.g.
     Object Identifier 0,0 must stay `c4 00 00 00 00`). They use the plain
     fixed-width big-endian `put_be()` instead.

Verification: an old-versus-new differential run of the test driver over about
460,000 commands produced identical output. The commands covered every encoder,
integer boundaries at each octet width, random and NaN/Inf REAL and Double bit
patterns, all 256 tag numbers, the extended-length boundaries and capacities
0-13, 20, 260 and 70000. The same corpus also passed under ASan/UBSan.
`acceptance.vec` gained 11 regression blocks: NaN rejection, Inf and -0, Boolean
FALSE, context Boolean, extended and reserved tag numbers, minimal-length
integer boundaries, extended lengths, and capacity errors. All 34 blocks pass
with both the old and the new build. `driver.c` and `run_vectors.py` were not
changed.

Unsure:
- NaN detection now assumes IEEE-754 binary32/binary64 bit layouts, with `double`
  in the same byte order as `uint64_t`. This holds for Cortex-M0 with GCC/Clang
  (EABI) and for the host build. Legacy ARM FPA mixed-endian doubles are not
  supported (the decoder already made the same assumption).
- Flash savings were measured with clang only. The final link on the real
  arm-none-eabi-gcc toolchain was not tested here.

## CR-103

Items 1 and 2 were implemented. Item 3 was not: it conflicts with the BACnet
standard (details below).

1. `bac_enc_char_string()` rejects text that is not well-formed UTF-8: **implemented**.
   - A new `utf8_well_formed()` checks the text against RFC 3629 section 4 before
     anything is written. It rejects (returns -1) stray continuation octets
     (80..BF), sequences cut off at the end of the text, a non-continuation
     octet inside a sequence, overlong forms (C0, C1, E0 80..9F, F0 80..8F),
     surrogates U+D800..U+DFFF (ED A0..BF, so CESU-8 surrogate pairs are
     rejected too), and code points above U+10FFFF (F4 90..BF, F5..FF).
     On an error the buffer is left unchanged, as for the other errors.
   - The empty string is still accepted. So is U+0000, which is well-formed
     UTF-8, and U+0000 is still written as the single octet 00. The "modified
     UTF-8" form C0 80 is now rejected.
   - It uses only integer code and is O(len). The comment in `bacapp.h` now
     documents the rejection.
   - Verified exhaustively against a separate decode-based reference
     implementation: every 1-, 2-, 3- and 4-octet string, plus about
     2.1 billion 5-octet strings, with no mismatches. A second check ran
     20,000 random strings through `drv` and compared the results with
     Python's strict UTF-8 decoder: no mismatches, and no `ERR DIRTY` or
     `OVERRUN` at any capacity.
   - Unsure:
     - Any caller that passed Latin-1, CESU-8 or modified UTF-8 text now gets
       -1 where it used to get an encoding. This is intended, but the
       integration team should check its callers.
     - A byte-order mark (EF BB BF) and noncharacters such as U+FFFE and
       U+FFFF are well-formed under RFC 3629, so they are still accepted.
     - The decoder was not changed. `bac_dec_app()` still returns received
       character strings without validating them, because the CR only covers
       the encoder and a received string can use a character set other than
       UTF-8.
2. NaN is encoded by `bac_enc_real()` and `bac_enc_double()`: **implemented**.
   - This does not conflict with the standard. Clauses 20.2.6 and 20.2.7 encode
     the IEEE-754 single and double formats, and NaN is a value in those formats.
     No clause excludes it, and the standard itself uses NaN as a property value
     (for example, the Averaging object's Average_Value when there are no valid
     samples). There was no documented product requirement for rejecting NaN
     either. The rejection was a consistency choice made in CR-101 and kept
     unchanged by the CR-102 refactor.
   - Both NaN checks were removed. The bit pattern is now encoded unchanged for
     every value: quiet and signalling NaN, either sign, any payload. This
     supersedes the NaN rejection described under CR-101 and CR-102.
     Infinities, negative zero and all other values encode exactly as before,
     and capacity errors are unchanged. The decoder already passed NaN through,
     so a NaN now round-trips bit for bit.
   - `acceptance.vec` changes: the CR-102 blocks `real-nan-rejected` and
     `double-nan-rejected` were renamed to `real-nan-encoded` and
     `double-nan-encoded`, and their expectations changed from `ERR` to the
     encoded bytes. A `nan-round-trip` block was added.
   - The CR-102 constraint still holds: there is no floating-point arithmetic,
     comparison or `<math.h>`. A clang build for `thumbv6m-none-eabi`
     (`-mfloat-abi=soft`) still references only `memcpy` and `memset`. The
     module's `.text` at `-Os` went from 2304 to 2452 bytes. The UTF-8 check
     adds more code than removing the NaN tests saves.
   - Differential check: 100,000 commands were run through the old and the new
     builds. They covered random REAL and Double bit patterns (about 24,000 of
     them NaN) and the other encoders and decoders. The only differences were
     the NaN results, and each one matched the expected `44`/`55 08` + bits
     encoding.
   - Unsure:
     - Signalling-NaN bits are preserved when `float`/`double` is passed in
       integer registers (Cortex-M0 soft-float) or SSE registers (x86-64 host,
       verified with `7f800001`). On an ABI that passes floats through the x87
       stack, the caller's hardware may quiet an sNaN before this module sees it.
       That is outside this module.
     - This module now transmits NaN faithfully. Whether a receiving device
       interprets NaN as "no sample" is a convention of the trend-log service
       and is not defined by BACnet. BACnet Trend Log records have their own ways
       to mark gaps (log-status and failure entries). The trend-log team may want
       to confirm that their peers read NaN the way they intend.
3. Encode Unsigned/Enumerated 0 with zero contents octets (`20` / `90`):
   **not implemented, because it conflicts with the BACnet standard.**
   - ASHRAE 135 clause 20.2.4 (Unsigned) and clause 20.2.11 (Enumerated) require
     the encoding to be primitive "with at least one contents octet", in the
     fewest octets possible. For 0 that is one octet, 00. The standard's own
     example for Enumerated 0 is X'91' X'00', and it has been in
     `acceptance.vec` as `enumerated-0` since before this CR. `20` and `90`
     would be malformed. Conforming receivers, including our own
     `bac_dec_app()`, reject them. The same applies to the context-tagged forms
     (`09 00`, not `08`). The integrator may be thinking of Null, or of the
     application-tagged Boolean, which carry no contents octets.
   - No code change. The regression block `unsigned-enumerated-zero` records the
     standard's behavior: `21 00`, `91 00`, context `19 00`, and the decoder
     rejects `20` and `90`.

Other artifacts: `bacapp.h` gained comments only (the UTF-8 rejection, and
REAL/Double encoding every bit pattern). `driver.c`, `run_vectors.py`,
`Makefile` and `CR.md` were not changed. `acceptance.vec` now has 39 blocks
(34 + 5 new). All pass with the normal build, and all pass with an
ASan/UBSan build of the driver. `bacapp.c` also compiles without warnings
under `-Wpedantic -Wconversion -Wsign-conversion` and clang `-Weverything`,
with only style warnings disabled.

Unsure / noticed:
- The CR-102 notes say `-lm` was removed from the `Makefile`, but the
  `Makefile` in this directory still links `-lm`. This is harmless because
  nothing uses libm. I did not change it because it is outside this CR, but the
  `Makefile` and the notes disagree.
- The flash size was measured only with clang (see CR-102). arm-none-eabi-gcc
  was not available here.

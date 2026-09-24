## CR-101

Neither item conflicts with ASHRAE 135 clause 20.2 or with the product's existing behaviour, so both are implemented.

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double()` writes `55 08` followed by the IEEE-754 double, 8 octets, big-endian. This matches clause 20.2.7: 72.0 encodes as `55 08 40 52 00 00 00 00 00 00`. It returns -1 without touching the buffer when `cap < 10` or `buf` is NULL.
   - `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is exactly 8. Any other length is rejected. It still accepts a non-minimal length header such as `55 fe 00 08`, just as it does for the other types (see D1b).
   - Test changes: `tests/unit.vec` p0526 (`dec_app 5508` + 8 zero octets) used to expect `ERR`. It now expects `OK n=10 d 0000000000000000`, because rejecting tag 5 was the old behaviour. I added vectors `cr101-d*` for the encoder, decoder, capacity checks and NaN. I added the clause 20.2.7 example to `acceptance.vec` as `double-72` / `decode-double`.
   - Unsure: **NaN handling.** The CR says nothing about NaN. I made `bac_enc_double()` reject NaN (return -1) to match the earlier CR3 decision for `bac_enc_real()` (vectors tagged CR3nan). As with REAL, the decoder passes NaN bit patterns through unchanged. The standard itself does not forbid NaN, so the product owner should confirm that Double should follow REAL here. If not, remove the `isnan` check and vectors cr101-d11..d13.
2. **`bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: implemented.**
   - They mirror `bac_enc_ctx_unsigned()`. They use a context-class header with the tag number (the extended tag octet for tags 15..254) and the minimal content octets, unsigned for Enumerated and two's-complement for Signed. Tag 255 is rejected, as the standard reserves it and the other context encoders already reject it. The capacity checks leave the buffer untouched on error.
   - I added vectors `cr101-e*` and `cr101-s*` to `tests/unit.vec`.

`bacapp.h` and `driver.c` were already updated and I left them unchanged. `make` builds cleanly with -Wall -Wextra. `run_vectors.py acceptance.vec tests/unit.vec` gives 361/361, also with ASan/UBSan.

## CR-102

Neither item conflicts with ASHRAE 135 clause 20.2 or with the product's documented behaviour, so both are implemented. External behaviour is unchanged.

1. **No floating-point arithmetic, comparisons or `<math.h>`: implemented.**
   - `#include <math.h>` and both `isnan()` calls are gone. `bac_enc_real()` and `bac_enc_double()` now `memcpy` the value into a `uint32_t` / `uint64_t` and reject NaN (exponent all ones, fraction non-zero) with an integer test on the bits: `(bits & 0x7FFFFFFF) > 0x7F800000`, or the 64-bit equivalent `> 0x7FF0000000000000`. This is the same NaN set that `isnan()` matched, so the CR3nan / CR101 NaN decisions stand. Where the CR-101 note above says to "remove the `isnan` check", that now means this bit test in `bac_enc_double()`.
   - The decoder already used `memcpy` and needed no change. `float` / `double` still appear only as API types (parameters and the `bac_value_t` union in `bacapp.h`, which is unchanged). No arithmetic or comparison is done on them.
   - `Makefile`: dropped `-lm`, because nothing links against libm any more.
   - Check: I compiled `bacapp.c` with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft` at -O0, -Os and -O2. The old file referenced `__aeabi_fcmpun` and `__aeabi_dcmpun`. The new one references only `memcpy` and `memset`. With clang -Os, `.text` for `bacapp.o` went from 2460 to 2322 bytes.
2. **One shared tag-header writer and one minimal-length integer writer: implemented.**
   - `tag_header()` is the only code that forms a tag header. It covers application and context class, extended tag numbers (15..254), inline or extended length (254 / 255 forms), the Boolean value in L/V/T, and opening / closing tags. It replaces `hdr_len()` + `put_hdr()`, the hard-coded initial octets (`0x00`, `0x10|v`, `0x44`, `0xA4`, `0xB4`, `0xC4`) and the separate code in `enc_open_close()`. Called with `buf == NULL` it only measures, so length and bytes come from one piece of logic.
   - `put_min_int()` is the only minimal-length integer writer (unsigned, or two's complement). It replaces `uint_len()` + `sint_len()` and the duplicate `enc_uint()` / `enc_sint()`. All Unsigned / Signed / Enumerated encoders, application and context, go through `enc_int()` → `put_min_int()`.
   - `begin()` / `emit()` hold the single capacity check ("buffer untouched on error") and the single reserved-tag-255 check. Before, each context encoder had its own copy of that check.
   - Interpretation (please confirm): I read "all encoders share one minimal-length integer writer" as "every encoder that writes a minimal-length integer uses the same one". Fixed-width fields keep their fixed width, because clause 20.2 requires it: REAL (4), Double (8), Date / Time (4), Object Identifier (4), and the 2- and 4-octet extended-length fields. Encoding those minimally would change the output and break the standard.
- Verification: `make` builds cleanly with -Wall -Wextra (also clean with -pedantic -Wfloat-equal -Wdouble-promotion under gcc and clang). `run_vectors.py acceptance.vec tests/unit.vec` gives 377/377, also under ASan/UBSan. I compared the old and new builds command by command: about 170k randomized driver commands and a 15.9k-command boundary sweep, covering integer and tag boundaries, header length forms, capacities 0..cap+1 and NaN bit patterns. Every output matched, including the ERR / DIRTY / OVERRUN checks. `driver.c` and `run_vectors.py` are unchanged.
- Test changes: I appended vectors `cr102-r01..r07` and `cr102-d01..d07` to `tests/unit.vec`. They cover REAL / Double NaN and infinity boundaries, including NaNs whose only set fraction bits are in the high or low 32-bit word. I also added `cr102-b01..b02` for application Boolean true through the shared header writer. Every expected result was produced by the pre-CR-102 build.
- Unsure: the ~2 KB figure depends on the firmware lead's toolchain (GCC/newlib, link flags). I checked only this object with clang for Cortex-M0. The final image should be checked for any `__aeabi_f*` / `__aeabi_d*` pulled in by other modules.

## CR-103

I implemented items 1 and 2. I did not implement item 3, because it conflicts with ASHRAE 135 clause 20.2.

1. **`bac_enc_char_string` rejects text that is not well-formed UTF-8: implemented.**
   - A new static `utf8_well_formed()` checks the text against the RFC 3629 table before the capacity check. On failure the encoder returns -1 and leaves the buffer untouched. It rejects:
     - C0, C1 and F5..FF;
     - a continuation octet (80..BF) where a lead octet should be;
     - truncated sequences, and continuation octets outside 80..BF;
     - overlong forms (E0 80..9F, F0 80..8F);
     - surrogates (ED A0..BF, U+D800..U+DFFF);
     - code points above U+10FFFF (F4 90..BF).
   - U+0000 and noncharacters such as U+FFFE / U+FFFF are well-formed under RFC 3629, so the encoder accepts them. The code uses only integer operations, so the CR-102 no-float constraint still holds.
   - Check: I tested the encoder exhaustively on every input of 0 to 4 octets (4,311,810,305 inputs) against a separate decode-then-range-check reference. There were no mismatches, and the output bytes were correct for every accepted input. I also ran 200k random driver commands (mutated valid text, random bytes, long strings, capacities around the needed size) against Python's strict UTF-8 codec. There were no mismatches and no ERR DIRTY, also under ASan/UBSan.
   - Test changes: the five `CR3utf8` vectors pinned the old pass-through behaviour, so their expected result changed from `OK ...` to `ERR`:
     - p0209 `c0af` and p0215 `c1bf` (overlong);
     - p0212 `e282` (truncated);
     - p0213 `80` (stray continuation);
     - p0216 `e080af` (overlong).

     I added `cr103-u01..u47` (valid boundaries, invalid forms and capacity cases).
   - Unsure / not changed: `bac_dec_app()` still passes received character-string octets through without checking UTF-8. The CR names only the encoder, and rejecting a whole value from a peer is a separate decision. Callers that need valid text must check decoded strings themselves.
2. **REAL / Double encode NaN like any other value: implemented.**
   - `bac_enc_real()` and `bac_enc_double()` no longer have the NaN bit test. Every IEEE-754 bit pattern is now written unchanged, including the sign and payload of quiet and signalling NaNs. Only NULL / capacity errors return -1. This matches the decoder, which already passed NaN through, so NaN now round-trips.
   - No conflict: clauses 20.2.6 / 20.2.7 require only the IEEE-754 single / double format and do not exclude NaN. My CR-101 note already said so. This CR reverses the earlier product decision to reject NaN (the `CR3nan` vectors, the CR-101 note and the CR-102 note, which said "the CR3nan / CR101 NaN decisions stand"). Those notes are superseded on this point. The comments in `bacapp.c` and `bacapp.h` are updated.
   - Test changes: the 13 `CR3nan` vectors changed from `ERR` to `OK 44<bits>` / `OK 5508<bits>`:
     - p0159, p0161, p0163;
     - cr101-d11..d13;
     - cr102-r05..r07;
     - cr102-d04..d07.

     I added `cr103-n01..n16`: signalling NaNs and payloads, capacity checks, and encode-then-decode round trips.
   - Unsure: (a) This change also makes REAL / Double accept NaN for every caller, not just the trend log. If any other feature relied on the encoder refusing NaN, it now has to check for NaN itself. (b) BACnet already has a standard way to mark a missing trend-log sample: the `log-status` / `failure` choices of `BACnetLogRecord`. Peers may not read a NaN `real-value` as "no sample", so the trend-log team should confirm that interoperability is acceptable. (c) A signalling NaN arrives unchanged on Cortex-M0 (soft-float) and x86-64. On an ABI that passes `float` through x87 registers (i386), the caller may quiet it before the encoder sees it.
3. **Unsigned / Enumerated 0 encoded with zero content octets (`20` / `90`): not implemented. It conflicts with the standard.**
   - The integrator's premise is wrong:
     - Clause 20.2.4: "The encoding of an unsigned integer value shall be primitive, and shall contain at least one contents octet."
     - Clause 20.2.11 says the same for Enumerated values.
     - Clause 20.2.4 applies to the context-tagged forms too.
   - The standard's own clause 20.2.11 example encodes Enumerated 0 as `91 00`, and `acceptance.vec` block `enumerated-0` pins that encoding.
   - Emitting `20` / `90` would also break interoperability:
     - Our own `bac_dec_app()` rejects zero-length Unsigned / Enumerated values (D4).
     - A conforming peer would reject them too.
   - The integrator may be thinking of Null (`00`) or the application-tagged Boolean, whose value is in the L/V/T field. Neither applies to Unsigned or Enumerated.
   - Behaviour is unchanged: `put_min_int()` always writes at least one octet. I added a comment there citing the clauses. Vector p0007 (`CR3zero`, `enc_unsigned 0` → `OK 2100`) stays as it is. I added `cr103-z01..z10`, which pin `91 00` / `21 00` / context forms, the capacity edge (cap 1 → ERR) and the decoder's rejection of `20` / `90`.

Verification:
- `make` builds cleanly with -Wall -Wextra. `bacapp.c` is also clean with -pedantic -Wconversion -Wsign-conversion -Wfloat-equal -Wdouble-promotion under gcc and clang.
- `run_vectors.py acceptance.vec tests/unit.vec` gives 450/450 (377 existing, 18 of them with updated expectations, plus 73 new), also under ASan/UBSan. The pre-CR-103 code fails 56 of them, all NaN / invalid-UTF-8 cases.
- For Cortex-M0 (clang, thumbv6m, soft-float), the object still references only `memcpy` / `memset`. `.text` at -Os went from 2322 to 2458 bytes (+136, for the UTF-8 check).
- `driver.c`, `run_vectors.py` and `acceptance.vec` are unchanged.

Unrelated observation: the CR-102 note says `-lm` was dropped from the `Makefile`, but the current `Makefile` links with `-lm` again. It is harmless, because nothing uses libm, and I left it alone because it is outside this CR. Someone should confirm which `Makefile` is authoritative.

## CR-101

Double and more context encoders. I found no conflict with ASHRAE 135, so both
items are implemented. `make` builds cleanly and `acceptance.vec` passes
(28/28, including 9 new CR-101 blocks).

### Item 1: application-tagged Double (tag 5): IMPLEMENTED

- `bac_enc_double()` writes `55 08` and then the 8-octet IEEE-754 binary64 bit
  pattern, big-endian and bit-exact (72.0 -> `55 08 40 52 00 00 00 00 00 00`,
  which is the ASHRAE 135 clause 20.2.7 example; -0.0 keeps its sign bit). It
  follows G1: it needs `cap >= 10` and writes nothing when it returns -1.
- `bac_dec_app()` now decodes tag 5 into `v.d`. The content length must be
  exactly 8; any other length returns -1. This changes D2 [POL]: v1.0
  rejected tag 5. I took CR-101 from the product owner as the sign-off for
  that change. As with every other tag, the header may use a non-canonical
  extended length (D1b), so `55 fe 00 08 <8 octets>` is accepted.
- **Needs confirmation: NaN.** The CR does not mention NaN. E5a [POL] refuses
  NaN for Real. Its rationale (decision D-7: front-ends crash on NaN, and
  invalid readings go through Status_Flags/Reliability, "never as NaN on the
  wire") applies to Double just as much. So `bac_enc_double()` returns -1 for
  any NaN and encodes +/-Infinity normally. The decoder accepts NaN as-is, the
  same as D5 does for Real. If the product owner wants NaN doubles sent, that
  goes against D-7 and needs an explicit decision. The `double-nan-refused`
  vector in `acceptance.vec` records the current behaviour.
- **Portability assumption.** A new `_Static_assert` requires
  `sizeof(double) == 8`. The build now fails on toolchains where `double` is
  32-bit (for example avr-gcc by default, or `-fshort-double`). Without the
  check those toolchains would silently send wrong bytes. The code also
  assumes that `double` and `uint64_t` use the same byte order. That holds on
  all mainstream targets but not for legacy ARM FPA mixed-endian doubles.

### Item 2: `bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: IMPLEMENTED

- Both use context class with the given tag and the same content as the
  application form: minimal big-endian unsigned (E3) or minimal
  two's-complement (E4). They follow the same rules as
  `bac_enc_ctx_unsigned()`:
  - Context tag 255 returns -1 (G3).
  - Tags 15..254 use the extended tag octet.
  - There are no partial writes (G1).
- Examples: `enc_ctx_enum 1 256` -> `1a0100`, `enc_ctx_signed 5 -72` -> `59b8`,
  `enc_ctx_signed 1 -129` -> `1aff7f`.

### Other artifacts and open points

- `bacapp.c`: I updated the module contract, the rule references and the
  rule -> code map, and added rule comments at the new code.
  `bacapp.h` and `driver.c` were already updated for this CR, so I did not
  change them.
- `acceptance.vec` has new blocks for:
  - Double encode and decode
  - -0.0
  - NaN refusal
  - Capacity 9 (no partial write)
  - Bad Double length
  - Context Enumerated and Signed
  - Context tag 255
- **SPEC.md is not in this directory**, even though `bacapp.c` says it
  implements it, so I could not update it. The rule ids I used are provisional
  and must be added to SPEC.md, which should probably become v1.1:
  - E5d [STD]: Double encoding.
  - E5a [POL]: NaN refusal, extended to Double.
  - E12e [STD] and E12s [STD]: context Enumerated and context Signed.
  - D5d [POL]: Double decoding, length exactly 8, NaN accepted.
  - D2 [POL]: tag 5 is no longer rejected.

## CR-102

This CR is a refactoring for code size on the Cortex-M0 build. I found no
conflict with SPEC.md or ASHRAE 135, so both items are implemented. External
behaviour is unchanged: no rule, return value or output byte changed. `make`
builds cleanly with no warnings, and `acceptance.vec` passes 37/37 (the 28
existing blocks plus 9 new CR-102 regression blocks).

### Item 1: no floating point, no `<math.h>`: IMPLEMENTED

- `isnan()` was the only floating-point operation in the file. It is gone,
  along with `#include <math.h>`. E5a [POL] (NaN refusal) is kept and is now a
  bit-pattern test on the value copied with `memcpy()`:
  - Real: NaN iff `(bits & 0x7FFFFFFF) > 0x7F800000`.
  - Double: NaN iff the exponent is all ones and the fraction is non-zero.
    The test works on the high and low 32-bit words, so there is no 64-bit
    compare either.
  - +/-Infinity is still encoded, and the decoders still accept NaN (D5, D5d).
- The `Makefile` no longer links `-lm`.
- **Verified on the M0 target.** I compiled `bacapp.c` with
  `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft`
  (clang 18; arm-none-eabi-gcc is not installed here):
  - Before: the object referenced `__aeabi_fcmpun` and `__aeabi_dcmpun`.
  - After: it references only `memcpy` and `memset`, at -O0, -O2 and -Os.
  - On x86-64 no floating-point compare instructions remain. The only xmm
    use left is moving the argument's bits into an integer register.
- `float`/`double` still appear as API parameter and union member types in
  `bacapp.h`, but the code only copies them. With the soft-float ABI they
  travel in core registers, so they need no FP code.
- **New build check.** I added `_Static_assert(sizeof(float) == 4)`, next to
  the existing one for `double`. The bit-pattern NaN test assumes IEEE-754
  binary32/binary64. The bit-exact copies of E5/E5d already assumed that, so
  the only new restriction is that `float` must be 4 bytes.

### Item 2: one tag-header writer, one minimal-length integer writer: IMPLEMENTED

- **`put_hdr()` is the only tag-header writer.** All 19 encoders now use it.
  Before this CR:
  - Null, Boolean, Real, Double, Date, Time and Object Identifier hard-coded
    their tag octets (`00`, `1x`, `44`, `55 08`, `A4`, `B4`, `C4`).
  - Opening/closing tags had their own copy of the extended-tag logic in
    `enc_open_close()`.
  - `hdr_len()` was a second copy of the header-size logic that had to be
    kept "in exact agreement" with `put_hdr()`.

  All three are gone. `put_hdr()` builds the header in a local buffer and
  checks header plus content against `cap` (G1). Only then does it copy the
  header into `buf`. Forms `HDR_BOOL` (E2, value in LVT) and
  `HDR_OPEN`/`HDR_CLOSE` (G5, LVT 6/7) cover the headers that carry no
  content length. The G3 check for context tag 255 is also made once, in
  `put_hdr()`. Before, five functions repeated it.
- **`put_int(p, v, sgn)` is the only minimal-length integer writer** (E3
  unsigned, E4 signed). It replaces `uint_len()`/`sint_len()` and
  `enc_uint()`/`enc_sint()`, which are now `put_int()` plus `enc_int()`.
  Unsigned, Enumerated and Signed use it, and so do their context forms
  (E12, E12e, E12s).
- **How I read "all encoders".** I read it as "every encoder that writes a
  minimal-length integer". Applied literally to every encoder, it would
  conflict with ASHRAE 135:
  - Real, Double, Date, Time and Object Identifier have fixed-length content
    (E5, E5d, E9, E10, E11). For example, Real 0.0 must be
    `44 00 00 00 00`, not `41 00`.
  - The G4 extended length uses only the 1-, 2- and 4-octet forms. A
    "minimal" 3-octet length would be invalid.

  These encoders keep the fixed-width big-endian store `put_be()`, which
  both writers also use. If the firmware lead meant something else, please
  clarify.

### Verification and other artifacts

- **`acceptance.vec`.** There are 9 new `cr102-*` blocks: NaN bit patterns
  for Real and Double, integer length boundaries, extended context tags,
  extended opening/closing tags, the G4 1/2/4-octet length forms, the
  fixed-length contents, and no partial write one byte short on each
  refactored path. I recorded their expected results with the pre-CR-102
  build, and that build passes them too.
- **Differential test** (a temporary harness, deleted afterwards).
  - I ran about 39,800 driver commands through the old and new builds and
    got byte-identical output. They covered every encoder, boundary and
    random values, capacities 0..12, tags 0..255, the G4 length boundaries
    up to 65536 octets, and random decoder input.
  - The same held for gcc -O0/-O2/-O3/-Os, clang -O0/-O2/-Os and gcc
    ASan+UBSan.
  - The driver cannot pass NULL `buf`/`data` pointers or lengths above
    0xFFFFFFFF, so I compared those paths by linking both versions
    together: 186 checks, all identical.
- **`bacapp.c` comments.** I updated the environment note, the module
  contract, the rule -> code map and the helper comments. No rule id is
  added or changed. `bacapp.h`, `driver.c` and `run_vectors.py` are
  unchanged.

### Unsure / open points

- **Size figures.** These come from clang 18 for thumbv6m, not from the
  production toolchain. The `.text` of `bacapp.o` changes like this:
  - At -Os it goes from 2460 to 2360 bytes, and the soft-float compare
    helpers are no longer needed.
  - At -O2 clang inlines `put_hdr()` into each encoder, and `.text` grows
    from 4016 to 4456 bytes.

  I do not know the M0 build's compiler or flags. If it builds at -O2, the
  gain is smaller than expected.
- **Where the ~2 KB saving comes from.** The saving appears only if no other
  module in the image uses floating point. Callers that compute `float`
  values before calling `bac_enc_real()` still pull in soft-float. This CR
  covers only `bacapp.c`.
- **Not tested on real targets.** I did not test on real Cortex-M0 hardware
  or on a big-endian target. The CR-101 assumption still applies: `double`
  and `uint64_t` have the same byte order.
- **SPEC.md is still not in this directory.** If its environment section
  says `isnan()`/`<math.h>` is used, it needs the same update as the
  `bacapp.c` header. No rule text changes.
- **External build systems.** If an M0 build file outside this directory
  adds `-lm` only for this module, that flag can be dropped. Keeping it does
  no harm.

## CR-103

CR-103 collects three field requests. I implemented item 1. I did not
implement items 2 and 3 because they conflict with the spec or the standard;
the reasons are below. `make` builds cleanly with no warnings, and
`acceptance.vec` passes 44/44 (the 37 existing blocks plus 7 new `cr103-*`
blocks).

### Item 1: `bac_enc_char_string` rejects text that is not well-formed UTF-8: IMPLEMENTED

- A new helper, `utf8_valid()`, checks the text against the RFC 3629
  section 4 syntax. `bac_enc_char_string()` returns -1 for:
  - truncated sequences, including one cut off at the end of the string
  - stray or out-of-range continuation octets
  - overlong forms (`C0`, `C1`, `E0 80..9F`, `F0 80..8F`)
  - surrogates (`ED A0..BF`, so U+D800..U+DFFF)
  - code points above U+10FFFF (`F4 90..BF`, `F5..FF`), which also covers
    the old 5- and 6-octet forms
- Text is only checked, never changed. The empty string, U+0000 (`00`), the
  BOM and noncharacters such as U+FFFF are well-formed and are still
  accepted.
- G1 still holds. The check runs before `put_hdr()`, so a rejected string
  leaves the buffer untouched even when it would have fit.
- The decoder does not change. D7 still accepts any character-string bytes
  and any charset octet, because the CR covers only the encoder.
- **This changes a [POL] rule.** In v1.0, E7a said "the bytes are NOT
  validated". E7a gave no rationale, and the new check matches character
  set 0 (UTF-8), which E7 [STD] already declares. The `bacapp.c` header
  asks for product-owner sign-off before any [POL] change. As with CR-101, I
  took the CR as that sign-off. CR-103 is a collection of team requests,
  though, not a request from the product owner. If it did not go through
  the product owner, E7a needs sign-off before release. The new E7a wording
  is PROVISIONAL until SPEC.md defines it.
- **Compatibility risk.** Any caller that now passes text that is not UTF-8
  gets -1 where it used to send the bytes. Examples are Latin-1 or other
  8-bit text, and a string cut to a fixed byte length in the middle of a
  multi-octet character, as with truncated object names. I did not audit
  the callers, which are outside this directory.
- **Verification.** I used a temporary harness and deleted it afterwards.
  - All 16,843,008 strings of 1 to 3 octets: every result matched Python's
    strict UTF-8 decoder.
  - All 2^32 strings of 4 octets: the encoder accepted exactly 383,270,912,
    which is the number of well-formed 4-octet strings worked out by
    counting.
  - 30,000 random mixed strings through `drv`, with various capacities, on
    the normal build and on an ASan+UBSan build: no mismatch and no dirty
    buffer.
  - gcc and clang at -O0, -O3 and -Os all pass 44/44.
  - On a build without the check, only the three invalid-UTF-8 blocks fail.
- **Size.** I used clang 18 with `thumbv6m`, Cortex-M0 and soft-float, as in
  CR-102. The object still references only `memcpy` and `memset`, so no
  floating point is pulled in. `.text` grows from 2360 to 2560 bytes at -Os
  and from 4456 to 4684 bytes at -O2.

### Item 2: encode NaN in `bac_enc_real` / `bac_enc_double`: NOT IMPLEMENTED (conflicts with E5a [POL])

- The request conflicts with E5a [POL] and its recorded rationale, decision
  D-7:
  - Our BMS front-end and two third-party workstations crash or show garbage
    when they receive NaN.
  - Invalid readings "must be reported through Status_Flags/Reliability,
    never as NaN on the wire".

  Using NaN to mean "no sample" is exactly what D-7 rules out. The CR-101
  notes already said that sending NaN doubles "goes against D-7 and needs an
  explicit decision". CR-103 comes from the trend-log team and cites no
  product-owner decision that replaces D-7.
- BACnet itself is not the obstacle: ASHRAE 135 allows NaN in a REAL. The
  conflict is with our own product requirement.
- **Suggestion for the trend-log team (please check against the standard).**
  As far as I know, BACnet trend logs already have a way to show a missing
  sample. In `BACnetLogRecord`, the `log-datum` choice includes `failure`
  (an Error), `null-value` and `log-status`, and the record also has an
  optional `status-flags`. These avoid NaN on the wire completely.
- If the product owner does overrule D-7, the code change is small: delete
  the two bit-pattern NaN checks. SPEC.md (E5a) and the vectors
  `double-nan-refused`, `cr102-*-nan-bit-patterns` and
  `cr103-nan-still-refused` would then need updating too.
- Behaviour is unchanged. I added comments at both checks that point to
  this decision. The `cr103-nan-still-refused` block records the current
  behaviour: Real and Double NaN are refused, and the decoders still accept
  NaN (D5, D5d).

### Item 3: encode Unsigned/Enumerated 0 as `20` / `90`: NOT IMPLEMENTED (conflicts with ASHRAE 135)

- The claim is wrong. ASHRAE 135 clause 20.2.4 (Unsigned) and 20.2.11
  (Enumerated, which is encoded like Unsigned) require the contents to use
  the fewest octets possible, and that is at least one. Value 0 is therefore
  one `00` octet: `21 00` and `91 00`, as E3 [STD] says. `acceptance.vec`
  lists `enc_enum 0` -> `9100` among its clause 20.2 examples. Changing
  this would also break interoperability:
  - Our own decoder rejects zero-length content (D4).
  - Other devices that follow the standard can be expected to reject it
    too.
- Behaviour is unchanged. I added a comment at E3. The
  `cr103-zero-has-one-content-octet` block covers the application and
  context forms and shows that `20` and `90` do not decode.
- If the integrator's real problem is a third-party device that *sends*
  `20`/`90`, that is a separate request: accepting zero-length content when
  decoding would relax D4 [POL]. It needs its own CR and product-owner
  decision.

### Other artifacts and open points

- `bacapp.c`:
  - The module contract, rule references and rule -> code map are updated.
  - E7/E7a now have the new text.
  - E5a and E3 have comments pointing to the declined items.
- `bacapp.h`: the comment on `bac_enc_char_string()` now states the UTF-8
  requirement. The prototype is unchanged.
- `driver.c` and `run_vectors.py` are unchanged.
- **SPEC.md is still not in this directory.** It needs:
  - the new E7a wording (UTF-8 validation on encode only; D7 unchanged)
  - ideally a note that the NaN (E5a/D-7) and zero-length Unsigned (E3)
    requests were declined
  - the CR-101 rule ids that are still outstanding
- **Makefile mismatch (not caused by this CR).** The `Makefile` still links
  `-lm`, but the CR-102 notes say it was removed. Linking `-lm` does no harm,
  because nothing uses `<math.h>`. I left the `Makefile` alone because it is
  outside this CR's scope. Either the `Makefile` or the CR-102 note should be
  corrected.

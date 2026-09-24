# NOTES

## CR-101

Neither item conflicts with ASHRAE 135 or with the documented [POL] rules, so
both are implemented in `bacapp.c`. `bacapp.h` and `driver.c` were already updated
and I did not change them. `make` builds cleanly, and `acceptance.vec` passes
23/23. That count includes 4 new CR-101 blocks: `double-72`, `decode-double`,
`context-enumerated` and `context-signed`.

### Item 1: application-tagged Double (tag 5): IMPLEMENTED
- `bac_enc_double()` writes `55 08` and then the 8 IEEE-754 octets, big-endian. The
  bit pattern is copied without conversion. This matches ASHRAE 135 clause
  20.2.7: tag 5, length 8, extended-length header. For example, 72.0 encodes as
  `55 08 40 52 00 00 00 00 00 00`. G1 also applies: if cap < 10, the function
  returns -1 and writes nothing.
- `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is
  exactly 8, and returns -1 for any other length. I changed rule D2, which rejected
  tag 5 in v1.0, and added rule D10. The CR from the product owner is the
  sign-off for that D2 [POL] change. Because of D1b, the decoder also accepts a
  non-canonical length header such as `55 fe 00 08`. It decodes NaN as-is, the
  same way D5 handles Real.
- **Unsure / needs product-owner confirmation (E15a):** the CR says nothing about
  NaN. I applied the Real NaN policy (E5a) to Double as well, so
  `bac_enc_double()` returns -1 for any NaN and still encodes +/-Infinity. The
  E5a rationale (decision D-7: "never as NaN on the wire") covers the problem
  of front-ends that crash on NaN, and that problem does not depend on precision.
  If Double should be allowed to send NaN, remove the NaN check in
  `bac_enc_double()`. Until CR-102 this check was an `isnan()` call. It is
  now `is_nan_bits()`.
- I added a C11 `_Static_assert(sizeof(double) == 8)`. The encoder and decoder
  copy the bits of a `double` directly, which is only correct when `double` is
  IEEE-754 binary64. On targets where `double` is 32-bit, the build now fails
  instead of producing wrong bytes on the wire.

### Item 2: `bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: IMPLEMENTED
- These work like `bac_enc_ctx_unsigned()`. The header is context class. The
  content is minimal big-endian unsigned (E3) for Enumerated and minimal
  two's-complement (E4) for Signed. Tags 15..254 use the extended tag form.
  Tag 255 returns -1 (G3). G1 applies: nothing is written on error. The new
  rule ids are E16 and E17.

### Open points
- `SPEC.md` is referenced by `bacapp.c` but is not in this directory, so I could
  not update it. It needs the following changes to match the code:
  - D2 no longer rejects tag 5.
  - New rules E15 (Double), E15a (NaN refused, [POL]), E16 (context
    Enumerated), E17 (context Signed) and D10 (Double decode: length exactly 8,
    bit-exact, NaN accepted, [POL]).

  These rule ids are my proposal, and the comments in `bacapp.c` say so.
- The new union member `double d` (from the updated `bacapp.h`) can change the
  alignment of `bac_value_t` on 32-bit targets, and its size through padding.
  For example, on ARM EABI the union goes from 4-byte to 8-byte alignment. This
  only affects callers that make assumptions about the struct's size or layout.

## CR-102

Neither item conflicts with ASHRAE 135 or with the documented [POL] rules, so
both are implemented in `bacapp.c`. No rule changes. The NaN refusal for Real
and Double (E5a, E15a) still applies; it is now an integer test on the bit
pattern. External behavior is unchanged: the decoders are textually identical,
and every encoder returns the same result and bytes as before. `make` builds
cleanly with no warnings, and `acceptance.vec` passes 31/31. That count
includes 8 new CR-102 regression blocks. Their expected values come from the
pre-CR-102 build, which also passes them. `driver.c` and `run_vectors.py` are
unchanged.

### Item 1: no floating point and no `<math.h>`: IMPLEMENTED
- `<math.h>` is removed. `isnan()` is replaced by `is_nan_bits()` in
  `bac_enc_real()` and `bac_enc_double()`. The encoders copy the value into a
  `uint32_t` or `uint64_t` with `memcpy()`. A value is NaN when its exponent
  field is all ones and its fraction is non-zero, whatever its sign and
  payload. +/-Infinity is still encoded. The decoders already used only
  `memcpy()`.
- I added `_Static_assert(sizeof(float) == 4)` next to the CR-101 check on
  `double`, because Real now depends on the float bit pattern in the same way.
- `Makefile`: I removed `-lm`, since nothing uses libm any more.
- I checked this with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0
  -mfloat-abi=soft -Os`. The object now references only `memcpy` and
  `memset`. Before this change it also pulled in `__aeabi_fcmpun` and
  `__aeabi_dcmpun`. On x86-64 (gcc -O2), the only FP-register instructions
  left move the `float`/`double` argument out of `xmm0`, as the ABI requires.
  There is no FP arithmetic or comparison.

### Item 2: one tag-header writer and one minimal-length integer writer: IMPLEMENTED
- `put_hdr()` is now the only code that builds a tag header, and all 19
  encoders use it. Before, Null, Boolean, Real, Double, Date, Time and Object
  Identifier wrote hard-coded header octets. Opening and closing tags had their
  own copy in `enc_open_close()`, which is gone.
- `put_hdr()` builds the header in a local buffer, then does the G1 check on
  header + content against `cap` before it writes anything. So the separate
  `hdr_len()`, which had to "stay in exact agreement" with `put_hdr()`, is
  removed. `put_hdr()` also does the G3 check that rejects tag 255. Before,
  five context encoders each had their own copy of that check.
- A `kind` argument covers the headers that carry no length: `HDR_FIXED | lvt`
  for the application Boolean (E2), and `HDR_OPEN`/`HDR_CLOSE` (G5).
- `enc_int()` is now the only minimal-length integer writer. It replaces
  `enc_uint()`, `enc_sint()`, `uint_len()` and `sint_len()`, and it serves
  Unsigned, Enumerated and Signed in both application and context class (E3,
  E4, E12, E16, E17). The signed length reuses the unsigned computation on
  `2 * (v < 0 ? ~v : v)`.
- How I read the CR: "minimal-length integer writer" applies to the
  integer-valued types. The contents of Real, Double, Date, Time and Object
  Identifier are fixed at 4 or 8 octets by ASHRAE 135. Writing them in minimal
  length would break conformance, so they still use fixed-width stores:
  `enc_be32()` for the 4-octet types, and `put_be()` for Double.

### Verification
- I ran a differential test of the old and new code with about 194,000 driver
  commands. They covered every encoder across the value boundaries, context
  tags 0..255, and capacities around each header-length boundary (5/253/254/
  65535/65536). The decoders got 20,000 random byte strings. Both versions gave
  byte-identical output, including with gcc + ASan/UBSan and with clang. The
  scratch harness is not kept in this directory.

### Unsure / open points
- I could not confirm the "about 2 KB" flash figure, because no
  `arm-none-eabi` toolchain or libgcc is available here to link an image. I
  only checked that `bacapp.o` no longer references soft-float helpers. Other
  modules in the image could still pull them in. The flash saving should be
  confirmed from the real map file.
- Code size depends on the optimization level. With clang for Cortex-M0,
  `.text` of `bacapp.o` went from 2460 to 2244 bytes at `-Os`, but from 4016 to
  4272 bytes at `-O2`, because more gets inlined. If the M0 build uses `-O2`,
  check the numbers there.
- `SPEC.md` is still missing from this directory. CR-102 changes no rule, but
  its environment section may mention `isnan()`/`<math.h>`, as the old header
  comment in `bacapp.c` did. If it does, it needs updating.
- The bit-pattern approach assumes that `float`/`double` have the same byte
  order as `uint32_t`/`uint64_t`. That holds on Cortex-M0 and x86. The CR-101
  Double code already relied on it.

## CR-103

None of the three items is implemented. Each one conflicts with a documented
rule, so the encoders behave exactly as before. Items 1 and 2 would reverse
[POL] rules. All three requests come from other stakeholders (the integration
team, the trend-log team and a system integrator), not from the product owner,
so none of them carries the sign-off that a [POL] change needs. For CR-101,
the product owner's CR was that sign-off. Item 3 would break ASHRAE 135.

The only code change is to comments in `bacapp.c`: short notes at E3, E5a,
E15a and E7a that record why each request was declined. `acceptance.vec` has 2
new regression blocks that pin the unchanged behavior:
`integer-zero-one-content-octet` and `char-string-bytes-not-validated`. NaN
refusal was already pinned by `real-nan-refused` and `double-nan-refused`.
`make` builds cleanly with no warnings, and `acceptance.vec` passes 33/33.
`driver.c` and `run_vectors.py` are unchanged.

### Item 1: reject malformed UTF-8 in `bac_enc_char_string()`: NOT IMPLEMENTED (conflicts with E7a [POL])
- The request is the opposite of rule E7a [POL]: "the bytes are NOT validated
  (no UTF-8 well-formedness check); they are sent exactly as given". It would
  also change the module contract ("character-string bytes are not
  validated"). A [POL] rule may only change with product-owner sign-off, and
  the integration team cannot give it.
- The request does not conflict with ASHRAE 135. Character set 0 is UTF-8, so
  checking that the text is well-formed is consistent with the standard. SPEC.md
  v1.0 records no rationale for E7a, which makes this the easiest of the three
  to approve. It is still a behavior change for every caller: any string that
  passes today but is not well-formed UTF-8, such as a Latin-1 object name from
  an old configuration, would start returning -1. The product owner needs to
  decide it.
- If it is approved, the change is small. Add a UTF-8 check (RFC 3629 /
  Unicode Table 3-7) that runs before `put_hdr()`, so that G1 still holds.
  Replace E7a in SPEC.md and in the comments. Change the
  `char-string-bytes-not-validated` vectors to `ERR`. The decoder (D7) can stay
  lenient.

### Item 2: encode NaN in `bac_enc_real()` / `bac_enc_double()`: NOT IMPLEMENTED (conflicts with E5a/E15a [POL])
- E5a and E15a [POL] refuse NaN on purpose. The recorded reason is decision
  D-7: our BMS front-end and two third-party workstations crash or show
  garbage when they receive NaN, so invalid readings must be reported through
  Status_Flags/Reliability, "never as NaN on the wire". The request contradicts
  that directly, and the trend-log team cannot override it.
- ASHRAE 135 would allow NaN in a Real or Double, so only the policy stands in
  the way. However, "no sample" in a trend log does not need NaN. As I
  understand the standard, a `BACnetLogRecord`'s log-datum is a CHOICE that
  includes `null-value` and `failure` (and `log-status`), and the record has
  `status-flags`. The trend-log service could use those instead.
- If the product owner does want NaN on the wire, E5a/E15a and decision D-7
  must be revisited, including the known client crashes. The change would be
  to remove the `is_nan_bits()` checks, and possibly `is_nan_bits()` itself,
  and to change the two `*-nan-refused` blocks.

### Item 3: Unsigned/Enumerated 0 as `20` / `90`: NOT IMPLEMENTED (conflicts with ASHRAE 135, E3 [STD])
- The integrator's reading of the standard is wrong. ASHRAE 135 requires at
  least one content octet for an Unsigned (clause 20.2.4) and for an
  Enumerated (clause 20.2.11). So 0 is `21 00` / `91 00`, as E3 already
  specifies. The same applies to the context forms (E12, E16), for example
  `39 00`. `20` / `90` is not conformant, and conformant receivers can reject
  it. Our own decoder does (D4 requires length 1..4), and the new vectors
  `dec_app 20` / `dec_app 90` show that.
- The existing `enumerated-0` vector (`9100`) and the new
  `integer-zero-one-content-octet` block pin the correct encoding.
- Suggested reply to the integrator: ask which device or tool expects `20` /
  `90`. That device is probably the non-conformant one.

### Unsure / open points
- I quoted the clause numbers (20.2.4, 20.2.11) and the `BACnetLogRecord`
  structure from memory. There is no copy of ASHRAE 135 in this directory. The
  rule itself (at least one content octet) is the one E3 [STD] already records.
- Treating items 1 and 2 as conflicts rests on who sent them. If the product
  owner endorses either one, it stops being a conflict and can be implemented
  as described above. Item 1 in particular has no recorded rationale behind
  E7a.
- `SPEC.md` is still not in this directory. CR-103 changes no rule, so it needs
  no update for this CR.
- The CR-102 notes say `-lm` was removed from the `Makefile`, but the current
  `Makefile` still links with `-lm`. It is harmless, because nothing uses libm.
  I left it alone because it is outside the scope of CR-103. The notes and the
  `Makefile` should be reconciled.

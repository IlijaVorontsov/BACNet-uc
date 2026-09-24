## CR-101

1. **Application-tagged Double (tag 5) — implemented.**
   - `bac_enc_double` writes `55 08` + the 8 octets of the IEEE-754 binary64 bit pattern,
     big-endian, bit-exact (-0.0, ±Infinity, subnormals preserved). Returns -1 without
     touching `buf` when `cap < 10` (G1). `55 08` is the standard encoding (length 8 > 4,
     so LVT = 5 plus one length octet, G4); matches ASHRAE 135 clause 20.2 example
     72.0 → `55 08 40 52 00 00 00 00 00 00`.
   - `bac_dec_app` now decodes tag 5 with content length exactly 8 into `v.d`; any other
     length → -1. NaN is decoded like any other value (as D5 does for Real). Lenient
     non-canonical headers (D1b, e.g. `55 fe 00 08 ...`) are accepted as for other tags.
   - SPEC.md bumped to v1.1: new rules E15 / E15a, D2 no longer rejects tag 5, D5 covers
     Double. acceptance.vec gained `double-72` and `decode-double`.
   - **Needs product-owner confirmation — NaN:** the CR does not mention NaN. Policy E5a /
     decision D-7 ("never NaN on the wire", front-end and workstations crash on NaN) is
     worded for `bac_enc_real` only, but its rationale applies equally to Double, and
     letting Double send NaN would reopen exactly that field problem. I therefore made
     `bac_enc_double` refuse any NaN (returns -1) and recorded it as new [POL] rule E15a.
     If the product owner wants NaN allowed for Double, that is a D-7 change and needs an
     explicit sign-off.
   - Portability: the code carries the raw bit pattern of a C `double`, so a
     `_Static_assert(sizeof(double) == 8)` was added to `bacapp.c`. Toolchains with a
     32-bit `double` (e.g. avr-gcc default) will now fail to build rather than silently
     emit wrong octets. It is also assumed (not checked) that `double` and `uint64_t` share
     byte order, which holds on all current mainstream targets (not on legacy ARM FPA).

2. **`bac_enc_ctx_enumerated` / `bac_enc_ctx_signed` — implemented.**
   - Context class, given tag number (extended tag octet for 15..254), content as E3 /
     E4 (minimum octets, two's complement for Signed); context tag 255 → -1 (G3); no
     partial writes on insufficient `cap` (G1). Same code paths as the application
     encoders and `bac_enc_ctx_unsigned`.
   - SPEC.md: new rules E16 / E17. acceptance.vec gained `context-enumerated` and
     `context-signed`.

No item of CR-101 conflicts with the BACnet standard or the documented requirements;
the only policy point is the NaN handling for Double described above.
Verified with `make` and `python3 run_vectors.py acceptance.vec` (23/23 pass), plus
manual driver checks of edge cases (NaN/Inf/-0.0, cap 9/10, wrong Double lengths,
tags 14/15/254/255, INT32_MIN/MAX).

## CR-102

1. **No floating point / no `<math.h>` in `bacapp.c` — implemented.**
   - The only floating-point operations were the `isnan()` calls in `bac_enc_real` /
     `bac_enc_double` (on the M0 they pulled in `__aeabi_fcmpun` / `__aeabi_dcmpun`). The
     value is now `memcpy`'d into a `uint32_t` / `uint64_t` first and NaN is recognised from
     the bit pattern: `(bits & 0x7FFFFFFF) > 0x7F800000` (Double:
     `(bits & 0x7FFFFFFFFFFFFFFF) > 0x7FF0000000000000`), i.e. exponent all ones and fraction
     non-zero, any sign. That is exactly the set `isnan()` accepts, so E5a / E15a are
     unchanged: quiet and signalling NaN of any sign/payload → -1; ±Infinity, -0.0 and
     subnormals are encoded bit-exact as before. (Side effect: the check no longer depends
     on compiler FP semantics — with `-ffast-math` the old `isnan()` was folded away and NaN
     was sent; checked with gcc.)
   - `#include <math.h>` removed; `-lm` removed from the Makefile (nothing needs libm now;
     `driver.c` does not use it). The decoders already used `memcpy` only.
   - The `_Static_assert` now also requires `sizeof(float) == 4` (the Real paths copy a
     float into a `uint32_t`). Build-time only; no effect on any supported target.
   - Checked with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft` at
     -O0/-O1/-O2/-Os: the object's only undefined symbols are `memcpy` and `memset`
     (before: also `__aeabi_fcmpun`, `__aeabi_dcmpun`). The x86-64 object contains no FP
     compare/arithmetic instructions.

2. **One tag-header writer and one minimal-length integer writer — implemented.**
   - `put_hdr()` is now the only code that forms a tag header (G2–G5: class bit, extended
     tag number, LVT / extended length in shortest form, opening/closing LVT 6/7). Every
     encoder reaches it through `enc_begin()`, which also holds the common checks (NULL
     `buf`, context tag 255 → -1 per G3, header + content must fit in `cap` before anything
     is written per G1). This includes the encoders that used to hard-code their header:
     Null `00`, Boolean `10`/`11` (value passed as the LVT), Real `44`, Double `55 08`,
     Date `A4`, Time `B4`, Object Identifier `C4`, and the opening/closing tags (which had
     their own header code in `enc_open_close`). `hdr_len()`, a second copy of the G4
     length logic, is gone: the header is built once into a local 7-octet buffer and copied
     after the capacity check.
   - `put_int()` is the only minimal-length integer writer (unsigned, or two's complement
     when `sgn`); it replaces `uint_len` / `sint_len` / `enc_uint` / `enc_sint`. It serves
     Unsigned, Enumerated, Signed, their context variants, and the context Boolean (whose
     `00`/`01` content is the one-octet case).
   - Remaining helper `put_be()` is a fixed-width big-endian store (Real / Double / Object
     Identifier content, 2- and 4-octet extended lengths), not minimal-length logic.
     Decoders were not changed (the CR is about encoders).

External behavior is unchanged. Verified with `make` and `python3 run_vectors.py
acceptance.vec` (32/32 pass). acceptance.vec gained 9 regression blocks for the paths the
refactor touched (NaN variants, ±Inf, -0.0, capacity boundaries, integer-length
boundaries, header-only values, extended tags, extended lengths); their expected output
was produced by the v1.1 build, and v1.1 passes them too. In addition, ~25,800 driver
commands (every encoder with capacity sweeps around the required size, random and edge
values, lengths across all G4 boundaries up to 70000, tags 0..15/254/255, plus decoders)
gave byte-identical output against the v1.1 build, also under ASan/UBSan and `-ffast-math`
(scratch files not kept). SPEC.md is now v1.2: new section 4 "Implementation constraints"
(I1 no floating point, I2 shared writers); no encoding or decoding rule changed.

Neither item conflicts with the BACnet standard or SPEC.md. The NaN policy (E5a / E15a) is
kept; only the way NaN is detected changed.

Unsure / please check:
- **Code size.** I measured with clang 18 only (no arm-none-eabi-gcc here), looking at
  object `.text`, not a linked image. At -Os `bacapp.o` went from 2460 to 2248 bytes, on
  top of the soft-float compare routines no longer being linked. At -O2, clang inlines the
  shared helpers into each encoder and `.text` grows from 4016 to 4416 bytes. If the M0
  build does not use -Os, the net saving is smaller than expected. Please confirm with the
  real toolchain and linker map.
- The ~2 KB saving only happens if nothing else in the M0 image uses soft-float. Callers
  of `bac_enc_real` / `bac_enc_double` still pass `float` / `double`, which costs nothing
  on the soft-float ABI (integer registers), but any float arithmetic they do themselves
  will still link the routines.
- The byte-order assumption from CR-101 (a `double` and a `uint64_t` share byte order) is
  unchanged. It now applies to `float` / `uint32_t` too.

## CR-103

None of the three items was implemented, because each one conflicts with SPEC.md or with
ASHRAE 135. `bacapp.c`, `bacapp.h`, SPEC.md (still v1.2) and acceptance.vec are
unchanged. `make` and `python3 run_vectors.py acceptance.vec` still pass (32/32).

1. **Reject malformed UTF-8 in `bac_enc_char_string` (integration team): not
   implemented. It conflicts with policy rule E7a.**
   - E7a [POL]: "The character-string encoder does not validate the bytes (they are sent
     as given)." This item asks for the opposite. SPEC.md says a [POL] rule may not be
     changed without product-owner sign-off. The request comes from the integration team,
     and nothing in CR-103 shows product-owner approval.
   - This item conflicts only with our own policy, not with the standard. For character
     set 0 (ISO 10646, UTF-8), clause 20.2.9 says the content octets hold the characters
     encoded in that set. Checking the input would therefore bring the encoder closer to
     the standard. I recommend asking the product owner to replace E7a.
   - What would change after sign-off:
     - Replace E7a with a rule that rejects input that is not RFC 3629 UTF-8: truncated
       sequences, stray continuation bytes, C0/C1/F5..FF lead bytes, overlong forms,
       U+D800..U+DFFF and anything above U+10FFFF all return -1.
     - Do the check before `enc_begin()`, so `buf` stays untouched on error (G1).
     - Leave the decoder alone (D7 reports the bytes as received).
     - Add encoder vectors for each rejection class and for the edge values U+007F,
       U+0080, U+07FF, U+0800, U+FFFF, U+10000 and U+10FFFF.
   - Current behavior, which stays as it is for now: `enc_str c0af` (overlong),
     `eda080` (surrogate), `f4908080` (above U+10FFFF) and `e282` (truncated) are all
     encoded as given.
   - Unsure: whether a separate product-owner decision already covers this. If it does,
     this can be implemented as described above.

2. **Encode NaN in `bac_enc_real` / `bac_enc_double` (trend-log team): not implemented.
   It conflicts with policy rules E5a and E15a (decision D-7).**
   - E5a and E15a require -1 for any NaN. The recorded reason is that our BMS front-end
     and two third-party workstations crash or show garbage when they receive NaN.
     Invalid readings must be reported through Status_Flags/Reliability instead.
   - The standard allows NaN in REAL and Double, so this is a conflict with our policy
     only. Allowing NaN would mean reversing D-7, which needs product-owner sign-off, and
     it would reintroduce the field problem that D-7 was made to prevent.
   - The acceptance vectors `nan-refused-real` and `nan-refused-double` pin the current
     behavior.
   - Suggestion for the trend-log team: do not use NaN to mean "no sample". A
     BACnetLogRecord can say this directly: its log-datum can be null-value, failure
     (Error) or log-status, and the record carries status flags.

3. **Encode Unsigned/Enumerated 0 with no content octets (`20` / `90`, system
   integrator): not implemented. It conflicts with ASHRAE 135 and with rule E3 [STD].**
   - The integrator's reading of the standard is wrong. Clause 20.2.4 (Unsigned) and
     clause 20.2.11 (Enumerated) say the encoding "shall contain at least one octet".
     Zero is therefore `21 00` / `91 00`, which is what E3 states and what we send. The
     same applies to the context forms (E12, E16), for example `enc_ctx_unsigned 1 0`
     gives `19 00`.
   - A length of 0 carries a value in the tag octet only for Null and the application
     Boolean (E1, E2). That is probably the source of the confusion.
   - Sending `20` / `90` would also break interoperability: conforming peers, and our own
     decoder (D4 requires length 1..4), reject it. `dec_app 20` and `dec_app 90` return
     ERR today.
   - Unsure: if the integrator's device sends `20` / `90` itself, accepting that when
     decoding would be a separate leniency decision in the style of D1b, changing D4
     [POL]. The integrator did not ask for this and I have not implemented it.

Other observation (not part of CR-103, left unchanged): the Makefile still links `-lm`,
although the CR-102 notes say it was removed. Neither `bacapp.c` nor `driver.c` needs
libm, so the build works either way. Please check which Makefile is the intended one.

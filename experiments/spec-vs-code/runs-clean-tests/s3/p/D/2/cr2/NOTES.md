## CR-101

**Item 1: application-tagged Double (tag 5). Implemented.**
- `bac_enc_double` writes `55 08` and then the 8 octets of the IEEE-754 binary64 value, big-endian and bit-exact (72.0 gives `55 08 40 52 00 00 00 00 00 00`). It follows G1: when `cap < 10`, or when it returns -1 for any other reason, `buf` is not touched.
- `bac_dec_app` now accepts tag 5 when the content length is exactly 8 and decodes it into `v.d`. Any other length is an error, as is content that runs past `len`. Under D1b the non-canonical length forms are accepted, for example `55 fe 00 08`. A NaN received on the wire is decoded like any other value, the same as D5 for Real.
- **NaN is refused by `bac_enc_double` (returns -1).** The CR does not mention NaN, but policy E5a / decision D-7 says NaN must never be sent on the wire, because the BMS front-end and the third-party workstations cannot handle it. I extended E5a to Double rather than add a way around that policy. ±Infinity is encoded normally. *Product owner, please confirm.* If D-7 should not apply to Double, remove the `isnan` check and vectors c101-10..14 and c101-19.
- Added `_Static_assert(sizeof(double) == 8)` in `bacapp.c` (SPEC P1). Some small-MCU toolchains make `double` 32 bits by default, and on those the API cannot produce a real binary64. *Not sure* whether any of our targets are affected. The code also assumes `double` and `uint64_t` use the same byte order, which is true on every current target I know of.
- SPEC updated to v1.1. Added E5b and D5b. E5a now covers Double. D2 no longer rejects tag 5.

**Item 2: `bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`. Implemented.**
- These are the context-class versions of E3 and E4. They produce the minimum number of content octets, handle extended tag numbers per G3, return -1 for context tag 255, and follow G1 (no partial writes). They are recorded as SPEC E15 and E16. Nothing here conflicts with ASHRAE 135 or with our policies.

**Tests / artifacts**
- `tests/unit.vec`:
  - p0526 (`55 08` + 8 zero octets) now expects `OK n=10 d 0000000000000000` instead of ERR. The v1.0 D2 rule it tested has been removed by this CR.
  - p0528 and p0529 were re-tagged D5b. They still expect ERR.
  - Added c101-01..62, covering Double encode/decode, NaN, capacity, lengths, context Enumerated and context Signed.
- `acceptance.vec`: added double-72, decode-double, context-enumerated and context-signed.
- All 366 vectors pass, including a run under ASan/UBSan. `driver.c`, `run_vectors.py` and `bacapp.h` were not modified.

## CR-102

**Item 1: no floating point in `bacapp.c`. Implemented.**
- Removed `<math.h>` and both `isnan` calls. `bac_enc_real` and `bac_enc_double` now `memcpy` the value into a `uint32_t`/`uint64_t` and refuse NaN (E5a/D-7, unchanged) with an integer test: the bits without the sign bit are greater than those of +Infinity (`0x7F800000` / `0x7FF0000000000000`). That is exactly "exponent all ones, fraction non-zero", so the result is the same as `isnan` for every bit pattern. The decoders already used `memcpy` only.
- `put_be64` (a 64-bit shift by a variable amount) was removed. A Double is now written as two 32-bit halves, which also gets rid of `__aeabi_llsr` on M0.
- Checked by cross-compiling `bacapp.c` with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft` at -O0, -O2, -O3 and -Os. The only undefined symbols left are `__aeabi_memcpy` and `__aeabi_memclr`. Before the change there were also `__aeabi_fcmpun`, `__aeabi_dcmpun` and `__aeabi_llsr`. `.text` at -Os went from 2476 to 2420 bytes. I could not link against a real M0 toolchain/libgcc, so the ~2 KB flash saving is the lead's figure, not one I measured. The x86 build no longer contains `ucomiss`/`ucomisd`.
- The Makefile no longer links `-lm`, because nothing uses it.
- Added `_Static_assert(sizeof(float) == 4)` next to the existing Double assert, and extended SPEC P1 to cover it. The bit-pattern NaN test hard-codes the binary32 layout, which E5 already requires. The assert changes nothing on any target I know of. *Product owner: this is a [POL] wording change to P1, please confirm.*
- The public API still takes and returns `float`/`double` (`bacapp.h` is unchanged). With the soft-float ABI these are just passed in core registers and copied, so no soft-float routine is needed. *Not sure:* an application that does its own float maths will still link soft-float code. This CR only covers `bacapp.c`.

**Item 2: one tag-header writer and one minimal-length integer writer. Implemented.**
- `put_tag()` is now the only code that writes a tag header. It handles application/context class, extended tag numbers (G3), all length forms (G4), the application Boolean's value in the LVT (E2) and opening/closing tags (G5). It replaces `hdr_len`, `put_hdr`, `enc_open_close` and the hard-coded header octets in Null, Boolean, Real, Double, Date, Time and Object Identifier. Called with `p == NULL`, it only computes the length. Every encoder goes through `begin()`, which sizes header plus content, checks context tag 255 (G3) and capacity (G1), and only then writes. The five per-encoder `tag == 255` checks are gone.
- `put_int()` is now the only minimal-length integer writer. It replaces `uint_len`, `sint_len` and the separate `enc_uint`/`enc_sint`. It takes the signedness as a parameter, because merging the two cannot mean one length rule. Using the unsigned minimum for Signed would violate E4 [STD]: 128 would become `31 80`, which decodes as -128, and -1 would become `34 ff ff ff ff` instead of `31 ff`. So Signed keeps the two's-complement minimum inside the shared writer. This is the only point where the CR text could be read in a way that conflicts with the standard. Implemented as described, nothing in the CR is refused.
- Documented as SPEC P2/P3 (SPEC v1.2). External behavior (E*/D*/G*) did not change.

**Verification**
- `tests/unit.vec`: added c102-01..60. They cover NaN/±Inf/max-finite/−0 bit-pattern boundaries for Real and Double, every length boundary of the integer writer (Signed, Unsigned, Enumerated, context forms with extended tags), and every header form (Boolean value in LVT at exact capacity, context Boolean/opening/closing with extended tags, tag 255, 1-/2-/4-octet extended lengths). All expected outputs were produced by the pre-CR-102 build, and both the old and new builds pass them. No existing vector was changed.
- All 426 vectors pass, also under GCC ASan/UBSan.
- Differential test of old and new builds: 200,000 random encoder/decoder commands (random capacities included), identical output with no `DIRTY` or `OVERRUN`. Exhaustive comparison of old and new `bac_enc_real`, `bac_enc_signed` and `bac_enc_unsigned` over all 2^32 inputs, plus `bac_enc_double` over 2^32 patterns around the Inf/NaN exponent: no differences.
- `driver.c`, `run_vectors.py`, `bacapp.h` and `acceptance.vec` were not modified.

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

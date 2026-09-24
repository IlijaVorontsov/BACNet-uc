# bacapp — Specification v1.1

Module: encoding/decoding of BACnet application-tagged primitive values (ASHRAE 135,
clause 20.2) for our bare-metal controller firmware (C11, no heap, no stdio).
API: `bacapp.h`.

Rules are tagged **[STD]** (required by ASHRAE 135) or **[POL]** (a product decision
made by us; the rationale is given — do not change a [POL] rule without product-owner
sign-off).

## 1. General

- **G1 [POL] No partial writes.** Encoders never write outside `buf[0..cap)`. If the
  encoding does not fit into `cap` bytes, or any argument is invalid, the encoder returns
  -1 and leaves **every byte of `buf` unmodified**.
  *Rationale:* encoders are called directly on the transmit buffer of the MS/TP driver;
  a half-written value there has caused corrupted frames in the field (incident 2024-11).
- **G2 [STD] Tag octet.** Bits 7..4 tag number, bit 3 class (0 = application,
  1 = context), bits 2..0 LVT (length/value/type).
- **G3 [STD] Extended tag numbers.** Tag numbers 15..254 are encoded with bits 7..4 =
  1111 and the tag number in the next octet. Tag number 255 is reserved: encoders return
  -1 for context tag 255.
- **G4 [STD] Lengths.** Content length 0..4 is written in the LVT field. Longer contents
  use LVT = 5 followed by: one octet with the length for 5..253; octet 254 plus a 2-octet
  big-endian length for 254..65535; octet 255 plus a 4-octet big-endian length above that.
  Encoders always use the shortest form.
- **G5 [STD] Opening/closing tags.** Context class, LVT = 6 (opening) / 7 (closing), no
  length and no content. Extended tag numbers apply (tag 15 opening = `FE 0F`).

## 2. Encoders

| Rule | Tag | Encoding |
|---|---|---|
| E1 [STD] | Null | `00` |
| E2 [STD] | Boolean | value in LVT, no content: `10` / `11` |
| E3 [STD] | Unsigned, Enumerated | big-endian, minimum number of octets, at least one (0 → `21 00`, `91 00`) |
| E4 [STD] | Signed | two's complement, minimum number of octets, at least one (128 → `32 00 80`, -129 → `32 ff 7f`) |
| E5 [STD] | Real | 4 octets IEEE-754 single, big-endian, bit-exact (-0.0 → `44 80 00 00 00`) |
| E5b [STD] | Double | 8 octets IEEE-754 double (binary64), big-endian, bit-exact; content length 8 is written as extended length: `55 08` + 8 octets (72.0 → `55 08 40 52 00 00 00 00 00 00`, -0.0 → `55 08 80 00 00 00 00 00 00 00`) |
| E6 [STD] | Octet String | the bytes; empty allowed (`60`) |
| E7 [STD] | Character String | first content octet = character set 0 (UTF-8), then the bytes (`""` → `71 00`) |
| E8 [STD] | Bit String | first content octet = number of unused bits (0..7) in the last octet, then bits packed MSB-first; empty string → `81 00` |
| E9 [STD] | Date | year−1900, month, day, weekday; 255 = unspecified (year `BAC_YEAR_UNSPECIFIED` → `FF`) |
| E10 [STD] | Time | hour, minute, second, hundredths; 255 = unspecified |
| E11 [STD] | Object Identifier | 4 octets big-endian: type (10 bits) << 22 \| instance (22 bits) |

Policy rules for encoders:

- **E5a [POL] NaN is refused.** `bac_enc_real` and `bac_enc_double` return -1 for any
  NaN (quiet or signalling, any sign/payload). ±Infinity is encoded normally.
  *Rationale:* our BMS front-end and two third-party workstations crash or display
  garbage when they receive NaN (decision D-7). Invalid sensor readings must be reported
  through Status_Flags/Reliability, never as NaN on the wire.
- **E7a [POL]** The character-string encoder does not validate the bytes (they are sent as
  given).
- **E8a [POL]** Each `bits[i]` must be 0 or 1; any other value → -1.
  *Rationale:* catches callers that pass bit masks instead of bit arrays (bug seen in
  the Status_Flags code).
- **E9a [POL] Date validation.** Year 1900..2154 or `BAC_YEAR_UNSPECIFIED`; month 1..14
  (13 = odd months, 14 = even months) or 255; day 1..34 (32 = last day of month,
  33 = odd days, 34 = even days) or 255; weekday 1..7 (1 = Monday) or 255. Anything else
  → -1.
- **E10a [POL] Time validation.** hour 0..23, minute 0..59, second 0..59, hundredths
  0..99, each or 255. Anything else → -1.
- **E11a [POL]** Object type > 1023 or instance > 4194303 → -1 (never truncate).

Context encoders:

- **E12 [STD]** `bac_enc_ctx_unsigned`, `bac_enc_ctx_enumerated`: context class, given tag
  number, content as E3. `bac_enc_ctx_signed`: context class, given tag number, content
  as E4 (e.g. context tag 2, -129 → `2A FF 7F`).
- **E13 [STD]** `bac_enc_ctx_boolean`: context class, length 1, content `00`/`01`
  (unlike the application Boolean).
- **E14 [STD]** Opening/closing tags per G5.
- Context tag 255 → -1 for all context encoders (G3).

## 3. Decoders

`bac_dec_tag`:

- **D1 [STD]** Parses the header per G2–G5 and returns its length (1 + extended tag
  octet + extended length octets). For LVT 0..4 `lvt` is that value; for LVT = 5 `lvt` is
  the extended length. LVT = 5 is always treated as an extended length, whatever the tag
  number and class.
- **D1a [POL]** Returns -1 when: `len` is 0; the header is truncated (extended tag or
  length octets missing); the extended tag number octet is 255; an application-class
  header has LVT 6 or 7.
- **D1b [POL] Lenient parsing.** Non-canonical forms are accepted: an extended tag number
  octet below 15 (e.g. `F9 03` = context tag 3), and extended lengths that would fit a
  shorter form (e.g. `65 03`, `65 fe 00 10`). *Rationale:* "be liberal in what you
  accept" — several field devices send such encodings (interop issue list #12).
- Opening/closing tags set `opening`/`closing`; `lvt` is then not meaningful.

`bac_dec_app` decodes one application-tagged value and returns header + content length.
Bytes after the value are ignored.

- **D2 [POL]** Returns -1 for: a header error (D1a); context class (including opening
  and closing tags); application tag numbers 13, 14 and ≥ 15; content extending beyond
  `len`.
- **D3 [POL] Null and Boolean** use the raw LVT bits of the tag octet: Null requires raw
  LVT 0; Boolean requires raw LVT 0 (false) or 1 (true). Anything else (including
  `15 01`, which D1 would read as an extended length) → -1.
- **D4 [POL] Unsigned / Enumerated / Signed:** length 1..4, otherwise -1. Non-minimal
  encodings are accepted (`22 00 05` → 5; `32 ff ff` → -1). Signed values are
  sign-extended.
- **D5 [POL] Real:** length exactly 4. NaN is decoded like any other value (the NaN rule
  E5a restricts only what *we* send).
- **D5a [POL] Double:** length exactly 8, decoded bit-exact into `v.d`. NaN is decoded
  like any other value (as D5).
- **D6 [STD]** Octet String: any length including 0.
- **D7 [POL] Character String:** length ≥ 1; the charset octet is reported as-is (no
  transcoding, any value accepted); `data`/`len` exclude the charset octet.
- **D8 [POL] Bit String:** length ≥ 1; unused-bits octet must be 0..7; a length-1 bit
  string must have unused = 0; `nbits = (length − 1) · 8 − unused`.
- **D9 [POL] Date / Time / Object Identifier:** length exactly 4; the field values are
  **not** validated when decoding (any octet accepted). Date year octet 255 →
  `BAC_YEAR_UNSPECIFIED`, otherwise 1900 + octet.

## 4. Platform

- **P1 [POL]** The C `double` type must be IEEE-754 binary64 (8 octets); `bacapp.c`
  refuses to compile otherwise (`_Static_assert`). Toolchains with a 32-bit `double`
  cannot provide the Double encoder/decoder.

## Change history

- **v1.1 (CR-101):** application-tagged Double (E5b, D5a; tag 5 removed from the D2
  reject list; NaN refused per E5a); context-tagged Enumerated and Signed encoders (E12).

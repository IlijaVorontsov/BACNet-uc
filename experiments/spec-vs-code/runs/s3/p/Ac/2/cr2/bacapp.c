/* bacapp.c - BACnet application-layer primitive encoding
 *
 * Implements SPEC.md, "bacapp - Specification v1.0", as amended by CR-101
 * (Double, context Enumerated/Signed): encoding and decoding of
 * BACnet application-tagged primitive values (ASHRAE 135, clause 20.2) for our
 * bare-metal controller firmware. Public API: bacapp.h. Environment: C11, no
 * heap, no stdio; only memcpy()/memset() from <string.h> are used.
 *
 * No floating point (CR-102): the Cortex-M0 target has no FPU, and linking
 * the soft-float support routines costs about 2 KB of flash. This file
 * therefore uses no floating-point arithmetic, no floating-point comparisons
 * and no <math.h>. A float/double value is only ever copied, with memcpy(),
 * to or from an integer holding its IEEE-754 bit pattern; everything else
 * (the NaN checks E5a/E15a included) is done on that integer. Real (E5, D5)
 * requires `float` to be the 4-octet IEEE-754 binary32 type and Double (E15,
 * D10) requires `double` to be the 8-octet IEEE-754 binary64 type; the sizes
 * are checked at compile time.
 *
 * MODULE CONTRACT (summary of SPEC.md)
 *
 *   Encoders (bac_enc_*)
 *     - Write exactly one complete tagged value into buf[0..cap) and return the
 *       number of bytes written, or -1.
 *     - Return -1 when any argument is invalid (NULL buf or data pointer with a
 *       non-zero length, value out of range, context tag 255, ...) or when the
 *       encoding does not fit into cap bytes.
 *     - G1 [POL] No partial writes: never write outside buf[0..cap); on -1,
 *       EVERY byte of buf is left unmodified. Consequently every encoder does
 *       all validation and computes the full encoded size before its first
 *       store into buf. Rationale: encoders are called directly on the
 *       transmit buffer of the MS/TP driver; a half-written value there has
 *       caused corrupted frames in the field (incident 2024-11).
 *     - Always emit the shortest form: minimal tag header (G3, G4) and
 *       minimal integer contents (E3, E4).
 *     - Policy checks on top of ASHRAE 135: NaN refused for Real and Double
 *       (E5a, E15a), bits must be 0/1 (E8a), date/time fields range-checked
 *       (E9a, E10a), object identifier fields never truncated (E11a);
 *       character-string bytes are not validated (E7a).
 *
 *   Decoders (bac_dec_tag, bac_dec_app)
 *     - Read only buf[0..len); return the number of bytes consumed (header
 *       length for bac_dec_tag, header + content for bac_dec_app), or -1.
 *     - Lenient about non-canonical encodings (D1b, D4), strict about
 *       structure: truncation, reserved values and unsupported tags -> -1
 *       (D1a, D2, D3, D7, D8).
 *     - Field values of Date/Time/Object Identifier are not validated (D9);
 *       NaN reals and doubles are decoded as-is (D5, D10).
 *     - bac_dec_app ignores any bytes after the value; pointer fields in *out
 *       point into buf (nothing is copied).
 *
 * ENCODER STRUCTURE (CR-102)
 *
 *   Every encoder validates its arguments, then calls put_hdr() - the one
 *   tag-header writer, which also does the G1 size check - and only then
 *   stores its content octets. Every Unsigned, Enumerated and Signed value,
 *   application or context class, is written by enc_int() - the one
 *   minimal-length integer writer. There are no per-type copies of either.
 *   Fixed-size contents (Real, Double, Date, Time, Object Identifier) are not
 *   minimal-length integers: ASHRAE 135 fixes their size at 4 or 8 octets.
 *
 * RULE REFERENCES
 *
 *   Comments in this file cite the SPEC.md rule ids (G1..G5, E1..E17 with
 *   E5a/E7a/E8a/E9a/E10a/E11a/E15a, D1..D10 with D1a/D1b) at the code that
 *   implements each rule, tagged:
 *     [STD]  required by ASHRAE 135 - changing it breaks conformance.
 *     [POL]  a product decision made by us. Where SPEC.md gives a rationale
 *            it is quoted next to the code; where a [POL] comment gives no
 *            rationale, SPEC.md v1.0 records none.
 *   E15, E15a, E16, E17 and D10 (and the change to D2) come from CR-101.
 *   They are proposed ids: SPEC.md must be amended to match (see NOTES.md).
 *
 *   Rule -> code map:
 *     G1          every bac_enc_* (argument checks first; put_hdr() checks
 *                 the complete size before the first write)
 *     G2..G5      put_hdr(), bac_dec_tag()
 *     E1, E2      bac_enc_null(), bac_enc_boolean() (via enc_hdr_only())
 *     E3, E4      enc_int() (bac_enc_unsigned/enumerated/signed())
 *     E5, E5a     bac_enc_real() (+ is_nan_bits(), enc_be32())
 *     E6..E8a     bac_enc_octet_string() .. bac_enc_bit_string()
 *     E9..E11a    bac_enc_date() .. bac_enc_object_id() (+ enc_be32())
 *     E12, E13    bac_enc_ctx_unsigned() (via enc_int()), bac_enc_ctx_boolean()
 *     E14         bac_enc_opening_tag/closing_tag() (via enc_hdr_only())
 *     E15, E15a   bac_enc_double() (+ is_nan_bits())
 *     E16, E17    bac_enc_ctx_enumerated(), bac_enc_ctx_signed() (via enc_int())
 *     D1, D1a/b   bac_dec_tag()
 *     D2..D10     bac_dec_app()
 *
 * !! Do NOT change the behaviour of any [POL] rule without product-owner
 * !! sign-off. When a change is approved, update SPEC.md and these comments
 * !! together with the code.
 */
#include "bacapp.h"

#include <string.h>

/* E5 / D5 copy the bits of a float to/from a uint32_t, and E15 / D10 the bits
 * of a double to/from a uint64_t. That is only correct if float is the
 * 4-octet IEEE-754 binary32 type and double the 8-octet IEEE-754 binary64
 * type (on some small targets double is 4 octets). Fail the build rather than
 * put wrong bytes on the wire. */
_Static_assert(sizeof(float) == 4 && sizeof(uint32_t) == 4,
               "bacapp: Real (E5/D5) requires a 4-octet IEEE-754 float");
_Static_assert(sizeof(double) == 8 && sizeof(uint64_t) == 8,
               "bacapp: Double (E15/D10) requires an 8-octet IEEE-754 double");

/* Store the n (<= 4) low-order octets of v big-endian (most significant
 * first), the byte order of all multi-octet contents (E3, E4, E5, E9..E11,
 * E15) and of the extended length (G4). */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* ---- the tag-header writer ----
 *
 * G2 [STD] Tag octet:  bits 7..4 tag number | bit 3 class | bits 2..0 LVT
 *   class: 0 = application, 1 = context
 *   LVT (length/value/type): 0..4 = content length (for the application
 *   Boolean: the value, E2), 5 = extended length follows (G4),
 *   6 / 7 = opening / closing tag (G5, context class only).
 *
 * The kind argument of put_hdr() gives the bits of the tag octet that come
 * from neither the tag number nor the content length:
 *   HDR_APP, HDR_CTX   the class (G2); LVT = content length (G4).
 *   HDR_FIXED | lvt    LVT given explicitly (value 0..7), no length octets
 *                      and no content: the application Boolean (E2,
 *                      HDR_APP class) and the opening / closing tags
 *                      (HDR_OPEN / HDR_CLOSE, G5, E14).
 */
#define HDR_APP 0x00u
#define HDR_CTX 0x08u
#define HDR_FIXED 0x10u
#define HDR_OPEN (HDR_FIXED | HDR_CTX | 6u)
#define HDR_CLOSE (HDR_FIXED | HDR_CTX | 7u)

/*
 * The one tag-header writer (CR-102): the only code that forms a tag header.
 * It also performs the G1 size check for the complete value, so that no
 * encoder can write before the full size is known to fit.
 *
 *   tag    tag number; 255 is reserved (G3 [STD]) -> error. (Application
 *          tag numbers passed here are constants 0..12.)
 *   kind   HDR_APP, HDR_CTX, HDR_OPEN, HDR_CLOSE or HDR_FIXED | value.
 *   clen   number of content octets the caller will store after the header
 *          (0 for HDR_FIXED kinds). Must be <= 0xFFFFFFFF, the largest G4
 *          length form; callers reject longer contents as invalid arguments.
 *
 * Returns the header length (1..7) after writing the header to buf, or 0
 * with nothing written when tag is 255, buf is NULL, or the header plus clen
 * content octets do not fit into cap. On success the caller stores exactly
 * clen content octets at buf + (return value).
 */
static size_t put_hdr(uint8_t *buf, size_t cap, uint8_t tag, unsigned kind, size_t clen)
{
    uint8_t hdr[7]; /* tag octet, extended tag, extended length (up to 5) */
    size_t n = 1;
    /* G2 [STD]: bit 3 = class (1 = context, 0 = application); for the
     * HDR_FIXED kinds bits 2..0 already hold the LVT. */
    uint8_t b = (uint8_t)(kind & 0x0Fu);

    /* G3 [STD]: context tag 255 is reserved -> -1 in every context encoder. */
    if (tag == 255)
        return 0;
    if (tag < 15) {
        /* G2 [STD]: tag number in bits 7..4. */
        b |= (uint8_t)(tag << 4);
    } else {
        /* G3 [STD] Extended tag number (15..254): bits 7..4 = 1111, tag
         * number in the next octet (tag 15 opening -> `FE 0F`). */
        b |= 0xF0;
        hdr[n++] = tag;
    }
    if (kind & HDR_FIXED) {
        /* E2 [STD] Boolean value / G5 [STD] opening (6) or closing (7) tag:
         * LVT set above, no length octets. */
    } else if (clen < 5) {
        /* G4 [STD]: content length 0..4 goes directly into LVT. */
        b |= (uint8_t)clen;
    } else {
        /* G4 [STD]: LVT = 5 followed by the extended length, always in the
         * shortest form. */
        b |= 5;
        if (clen <= 253) {
            /* 5..253: one length octet. */
            hdr[n++] = (uint8_t)clen;
        } else if (clen <= 65535) {
            /* 254..65535: octet 254 + 2-octet big-endian length. */
            hdr[n++] = 254;
            put_be(hdr + n, (uint32_t)clen, 2);
            n += 2;
        } else {
            /* above 65535: octet 255 + 4-octet big-endian length. */
            hdr[n++] = 255;
            put_be(hdr + n, (uint32_t)clen, 4);
            n += 4;
        }
    }
    hdr[0] = b;
    /* G1 [POL]: the whole value (header + content) must fit before anything
     * is written (cap - n < clen rather than n + clen > cap, which could
     * overflow). */
    if (!buf || cap < n || cap - n < clen)
        return 0;
    memcpy(buf, hdr, n);
    return n;
}

/* A value that consists of the tag header alone: Null (E1), application
 * Boolean (E2), opening / closing tag (E14). */
static int enc_hdr_only(uint8_t *buf, size_t cap, uint8_t tag, unsigned kind)
{
    size_t h = put_hdr(buf, cap, tag, kind, 0);
    return h ? (int)h : -1;
}

/*
 * The one minimal-length integer writer (CR-102): writes every Unsigned,
 * Enumerated and Signed value, application class (E3, E4) and context class
 * (E12, E16, E17), header included.
 *
 *   E3 [STD] unsigned (is_signed false): big-endian, minimum number of
 *     octets, at least one (0 -> one octet: `21 00`, `91 00`).
 *   E4 [STD] signed (is_signed true, v = the int32_t value converted to
 *     uint32_t): two's complement, big-endian, minimum number of octets, at
 *     least one - the smallest n whose signed n-octet range holds the value
 *     (128 -> `00 80`, -129 -> `ff 7f`); the content is the n low-order
 *     octets of the 32-bit two's-complement value.
 *
 * A signed value s fits in n octets iff -2^(8n-1) <= s < 2^(8n-1), i.e. iff
 * m = 2 * (s < 0 ? ~s : s) (its magnitude plus a sign bit) fits in n
 * unsigned octets; so one length computation serves both cases.
 */
static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, unsigned kind, uint32_t v, bool is_signed)
{
    uint32_t m = v;
    size_t n = 1;
    if (is_signed)
        m = ((v & 0x80000000u) ? ~v : v) << 1;
    while (n < 4 && (m >> (8 * n)) != 0)
        n++;
    /* G1 [POL]: put_hdr() checks the full size before the first write. */
    size_t h = put_hdr(buf, cap, tag, kind, n);
    if (!h)
        return -1;
    put_be(buf + h, v, n);
    return (int)(h + n);
}

/* An application value with a 4-octet big-endian content (E5, E9, E10,
 * E11): the tag octet is tag << 4 | 4 (`44`, `A4`, `B4`, `C4`). */
static int enc_be32(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    /* G1 [POL]: put_hdr() checks the full size before the first write. */
    size_t h = put_hdr(buf, cap, tag, HDR_APP, 4);
    if (!h)
        return -1;
    put_be(buf + h, v, 4);
    return (int)(h + 4);
}

/*
 * NaN test on an IEEE-754 bit pattern, done with integer operations only
 * (CR-102; replaces isnan()). A value is NaN iff its exponent field is all
 * ones and its fraction is non-zero, whatever the sign and payload.
 *   hi   the 32-bit word holding sign, exponent and upper fraction bits
 *   lo   the remaining fraction bits (0 for a float)
 *   inf  hi of +Infinity: 0x7F800000 (float), 0x7FF00000 (double)
 */
static bool is_nan_bits(uint32_t hi, uint32_t lo, uint32_t inf)
{
    hi &= 0x7FFFFFFFu; /* ignore the sign bit */
    return hi > inf || (hi == inf && lo != 0);
}

/* ---- application encoders ----
 *
 * G1 [POL] No partial writes - applies to every encoder below: never write
 * outside buf[0..cap); if the encoding does not fit into cap bytes, or any
 * argument is invalid, return -1 and leave every byte of buf unmodified.
 * Hence each function validates its arguments first; put_hdr() then checks
 * the complete size against cap before the first store into buf.
 * Rationale: encoders are called directly on the transmit buffer of the MS/TP
 * driver; a half-written value there has caused corrupted frames in the field
 * (incident 2024-11).
 */

/* E1 [STD] Null: `00` (application tag 0, LVT 0, no content). */
int bac_enc_null(uint8_t *buf, size_t cap)
{
    return enc_hdr_only(buf, cap, BAC_TAG_NULL, HDR_APP);
}

/*
 * E2 [STD] Boolean: the value is carried in the LVT field, no content octet:
 * false -> `10`, true -> `11`. (The context Boolean differs, see E13.)
 */
int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    return enc_hdr_only(buf, cap, BAC_TAG_BOOLEAN, HDR_APP | HDR_FIXED | (v ? 1u : 0u));
}

/* E3 [STD] Unsigned: application tag 2, minimal big-endian content (0 -> `21 00`). */
int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_UNSIGNED, HDR_APP, v, false);
}

/* E3 [STD] Enumerated: application tag 9, encoded like Unsigned (0 -> `91 00`). */
int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_ENUMERATED, HDR_APP, v, false);
}

/* E4 [STD] Signed: application tag 3, minimal two's-complement content
 * (128 -> `32 00 80`, -129 -> `32 ff 7f`). */
int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return enc_int(buf, cap, BAC_TAG_SIGNED, HDR_APP, (uint32_t)v, true);
}

/*
 * E5 [STD] Real: application tag 4, length 4 (tag octet 0x44), then the
 * IEEE-754 single-precision bit pattern, big-endian, bit-exact: the bits are
 * copied, not converted (-0.0 -> `44 80 00 00 00`).
 */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    /* CR-102: only the bit pattern is used - no floating-point operation. */
    memcpy(&bits, &v, sizeof bits);
    /*
     * E5a [POL] NaN is refused: any NaN (quiet or signalling, any sign or
     * payload) -> -1. +/-Infinity is NOT refused; it is encoded normally.
     * Rationale: our BMS front-end and two third-party workstations crash or
     * display garbage when they receive NaN (decision D-7). Invalid sensor
     * readings must be reported through Status_Flags/Reliability, never as
     * NaN on the wire.
     * This restricts only what we send; the decoder accepts NaN (D5).
     */
    if (is_nan_bits(bits, 0, 0x7F800000u))
        return -1;
    return enc_be32(buf, cap, BAC_TAG_REAL, bits);
}

/*
 * E15 [STD] Double (CR-101): application tag 5, length 8. The length does not
 * fit in LVT (G4), so the header is the extended-length form `55 08`,
 * followed by the IEEE-754 double-precision bit pattern, big-endian,
 * bit-exact: the bits are copied, not converted
 * (72.0 -> `55 08 40 52 00 00 00 00 00 00`, -0.0 -> `55 08 80 00 .. 00`).
 */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    /* CR-102: only the bit pattern is used - no floating-point operation. */
    memcpy(&bits, &v, sizeof bits);
    uint32_t hi = (uint32_t)(bits >> 32);
    uint32_t lo = (uint32_t)bits;
    /*
     * E15a [POL] NaN is refused, as for Real (E5a): any NaN (quiet or
     * signalling, any sign or payload) -> -1. +/-Infinity is NOT refused.
     * Rationale (E5a, decision D-7): our BMS front-end and two third-party
     * workstations crash or display garbage when they receive NaN; invalid
     * readings must be reported through Status_Flags/Reliability, never as
     * NaN on the wire.
     * This restricts only what we send; the decoder accepts NaN (D10).
     */
    if (is_nan_bits(hi, lo, 0x7FF00000u))
        return -1;
    /* G2/G4 [STD]: application tag 5, LVT 5, length octet 8 -> `55 08`.
     * G1 [POL]: put_hdr() checks the full size (10) before the first write. */
    size_t h = put_hdr(buf, cap, BAC_TAG_DOUBLE, HDR_APP, 8);
    if (!h)
        return -1;
    put_be(buf + h, hi, 4);
    put_be(buf + h + 4, lo, 4);
    return (int)(h + 8);
}

/*
 * E6 [STD] Octet String: application tag 6, length = len, then the bytes as
 * given. Empty is allowed (-> `60`).
 */
int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    /* Invalid arguments: a length beyond the largest G4 length form
     * (4 octets), or no data for a non-empty string. */
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    /* G1 [POL]: put_hdr() checks the whole encoding before anything is
     * written. */
    size_t h = put_hdr(buf, cap, BAC_TAG_OCTET_STRING, HDR_APP, len);
    if (!h)
        return -1;
    if (len)
        memcpy(buf + h, data, len);
    return (int)(h + len);
}

/*
 * E7 [STD] Character String: application tag 7; content = one character-set
 * octet, 0 (UTF-8), followed by the len bytes (`""` -> `71 00`).
 *
 * E7a [POL]: the bytes are NOT validated (no UTF-8 well-formedness check);
 * they are sent exactly as given.
 */
int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    /* Invalid arguments: content length len + 1 (charset octet) must fit the
     * largest G4 length form (4 octets), hence >=; no text for len > 0. */
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    size_t clen = len + 1;
    /* G1 [POL]: put_hdr() checks the whole encoding before anything is
     * written. */
    size_t h = put_hdr(buf, cap, BAC_TAG_CHARACTER_STRING, HDR_APP, clen);
    if (!h)
        return -1;
    buf[h] = 0x00; /* E7 [STD]: character set 0 = UTF-8 */
    if (len)
        memcpy(buf + h + 1, utf8, len);
    return (int)(h + clen);
}

/*
 * E8 [STD] Bit String: application tag 8; content = one octet giving the
 * number of unused bits (0..7) in the last octet, then the bits packed
 * MSB-first (bits[0] -> bit 7 of the first data octet). Empty string ->
 * `81 00` (only the unused-bits octet, value 0).
 */
int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    if (nbits && !bits)
        return -1;
    /*
     * E8a [POL]: each bits[i] must be 0 or 1; any other value -> -1 (checked
     * before any write, G1).
     * Rationale: catches callers that pass bit masks instead of bit arrays
     * (bug seen in the Status_Flags code).
     */
    for (size_t i = 0; i < nbits; i++)
        if (bits[i] > 1)
            return -1;
    size_t nbytes = (nbits + 7) / 8;
    /* Content length nbytes + 1 must fit the largest G4 length form. */
    if (nbytes >= 0xFFFFFFFFu)
        return -1;
    size_t clen = nbytes + 1;
    /* G1 [POL]: put_hdr() checks the whole encoding before anything is
     * written. */
    size_t h = put_hdr(buf, cap, BAC_TAG_BIT_STRING, HDR_APP, clen);
    if (!h)
        return -1;
    /* E8 [STD]: number of unused bits in the last octet (0..7). */
    buf[h] = (uint8_t)(nbytes * 8 - nbits);
    /* E8 [STD]: pack MSB-first; unused trailing bits are 0. */
    uint8_t *p = buf + h + 1;
    memset(p, 0, nbytes);
    for (size_t i = 0; i < nbits; i++)
        if (bits[i])
            p[i / 8] |= (uint8_t)(0x80 >> (i % 8));
    return (int)(h + clen);
}

/*
 * E9 [STD] Date: application tag 10, length 4 (tag octet 0xA4); content =
 * year - 1900, month, day, weekday; 255 = unspecified (year
 * BAC_YEAR_UNSPECIFIED -> `FF`).
 *
 * E9a [POL] Date validation - anything outside these ranges -> -1:
 *   year    1900..2154 (octet 0..254) or BAC_YEAR_UNSPECIFIED
 *   month   1..14 (13 = odd months, 14 = even months) or 255
 *   day     1..34 (32 = last day of month, 33 = odd days, 34 = even days)
 *           or 255
 *   weekday 1..7 (1 = Monday) or 255
 * Only these ranges are checked (no calendar-consistency check). All checks
 * precede the first write (G1).
 */
int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t y;
    /* E9 [STD] / E9a [POL]: year */
    if (year == BAC_YEAR_UNSPECIFIED)
        y = 255;
    else if (year >= 1900 && year <= 2154)
        y = (uint8_t)(year - 1900);
    else
        return -1;
    /* E9a [POL]: month 1..14 or 255 */
    if (!((month >= 1 && month <= 14) || month == 255))
        return -1;
    /* E9a [POL]: day 1..34 or 255 */
    if (!((day >= 1 && day <= 34) || day == 255))
        return -1;
    /* E9a [POL]: weekday 1..7 or 255 */
    if (!((wday >= 1 && wday <= 7) || wday == 255))
        return -1;
    /* E9 [STD]: content octets year, month, day, weekday in that order. */
    return enc_be32(buf, cap, BAC_TAG_DATE,
                    (uint32_t)y << 24 | (uint32_t)month << 16 | (uint32_t)day << 8 | wday);
}

/*
 * E10 [STD] Time: application tag 11, length 4 (tag octet 0xB4); content =
 * hour, minute, second, hundredths; 255 = unspecified.
 *
 * E10a [POL] Time validation: hour 0..23, minute 0..59, second 0..59,
 * hundredths 0..99, each or 255; anything else -> -1. All checks precede the
 * first write (G1).
 */
int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    /* E10a [POL] */
    if (!(hour <= 23 || hour == 255))
        return -1;
    if (!(minute <= 59 || minute == 255))
        return -1;
    if (!(second <= 59 || second == 255))
        return -1;
    if (!(hundredths <= 99 || hundredths == 255))
        return -1;
    /* E10 [STD]: content octets hour, minute, second, hundredths in that
     * order. */
    return enc_be32(buf, cap, BAC_TAG_TIME,
                    (uint32_t)hour << 24 | (uint32_t)minute << 16 | (uint32_t)second << 8 | hundredths);
}

/*
 * E11 [STD] Object Identifier: application tag 12, length 4 (tag octet
 * 0xC4); content = 4 octets big-endian: type (10 bits) << 22 | instance
 * (22 bits).
 */
int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    /* E11a [POL]: object type > 1023 or instance > 4194303 (0x3FFFFF) -> -1;
     * never truncate to the field width. */
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    return enc_be32(buf, cap, BAC_TAG_OBJECT_ID, ((uint32_t)type << 22) | instance);
}

/* ---- context encoders ----
 *
 * G3 [STD]: context tag 255 is reserved -> every context encoder returns -1
 * for it (before any write, G1); the check is made by put_hdr(). Tags
 * 15..254 use the extended tag form.
 * G1 [POL] (no partial writes) applies here as well.
 */

/* E12 [STD] Context Unsigned: context class, given tag number, content as
 * E3 (minimal big-endian unsigned). */
int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, HDR_CTX, v, false);
}

/* E16 [STD] Context Enumerated (CR-101): context class, given tag number,
 * content as E3 (minimal big-endian unsigned), i.e. encoded exactly like the
 * context Unsigned (E12). */
int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, HDR_CTX, v, false);
}

/* E17 [STD] Context Signed (CR-101): context class, given tag number,
 * content as E4 (minimal two's-complement, big-endian). */
int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return enc_int(buf, cap, tag, HDR_CTX, (uint32_t)v, true);
}

/*
 * E13 [STD] Context Boolean: context class, length 1, content `00` (false) /
 * `01` (true). Unlike the application Boolean (E2) the value is NOT carried
 * in LVT.
 */
int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* G1 [POL]: put_hdr() checks the full size before the first write. */
    size_t h = put_hdr(buf, cap, tag, HDR_CTX, 1);
    if (!h)
        return -1;
    buf[h] = v ? 1 : 0;
    return (int)(h + 1);
}

/*
 * E14 [STD] / G5 [STD] Opening / closing tag: context class, LVT = 6
 * (opening) or 7 (closing), no length and no content. Extended tag numbers
 * apply (G3): tag 15 opening -> `FE 0F`.
 */
int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_hdr_only(buf, cap, tag, HDR_OPEN);
}

/* E14 [STD]: closing tag, LVT 7 (G5). */
int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_hdr_only(buf, cap, tag, HDR_CLOSE);
}

/* ---- decoders ---- */

/*
 * D1 [STD] Parse one tag header per G2..G5 and return its length:
 * 1 + extended tag octet (if any) + extended length octets (if any).
 *   - LVT 0..4: out->lvt = that value.
 *   - LVT 5:    out->lvt = the extended length. LVT 5 is ALWAYS treated as an
 *               extended length, whatever the tag number and class.
 *   - LVT 6/7:  opening/closing tag -> out->opening / out->closing set;
 *               out->lvt is then not meaningful (left 0).
 *
 * D1a [POL] Returns -1 when: len is 0; the header is truncated (extended tag
 * or extended length octets missing); the extended tag number octet is 255;
 * an application-class header has LVT 6 or 7.
 *
 * D1b [POL] Lenient parsing: non-canonical forms are accepted - an extended
 * tag number octet below 15 (e.g. `F9 03` = context tag 3) and extended
 * lengths that would fit a shorter form (e.g. `65 03`, `65 fe 00 10`).
 * Rationale: "be liberal in what you accept" - several field devices send
 * such encodings (interop issue list #12). There is therefore deliberately
 * no canonical-form check in this function.
 */
int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    /* D1a [POL]: len 0 (or NULL arguments) -> -1 */
    if (!buf || !out || len < 1)
        return -1;
    size_t i = 1;
    /* G2 [STD]: split the tag octet into tag number, class and LVT. */
    uint8_t b = buf[0];
    uint8_t tag = (uint8_t)(b >> 4);
    bool ctx = (b & 0x08) != 0;
    uint8_t lvt = b & 0x07;
    if (tag == 15) {
        /* G3 [STD]: extended tag number in the next octet.
         * D1a [POL]: missing octet (truncated) or reserved value 255 -> -1.
         * D1b [POL]: values below 15 (non-canonical) are accepted as is. */
        if (len < 2 || buf[1] == 255)
            return -1;
        tag = buf[1];
        i = 2;
    }
    out->tag = tag;
    out->context = ctx;
    out->opening = false;
    out->closing = false;
    out->lvt = 0;
    if (lvt == 6 || lvt == 7) {
        /* G5 [STD]: opening (6) / closing (7) tag; no length, no content.
         * D1a [POL]: application class with LVT 6/7 -> -1. */
        if (!ctx)
            return -1;
        out->opening = (lvt == 6);
        out->closing = (lvt == 7);
        return (int)i;
    }
    if (lvt < 5) {
        /* G4 [STD] / D1: length (or value) 0..4 directly in LVT. */
        out->lvt = lvt;
        return (int)i;
    }
    /* G4 [STD] / D1: LVT = 5 -> extended length, for any tag number and
     * class. D1a [POL]: missing length octets (truncated) -> -1 below. */
    if (len < i + 1)
        return -1;
    uint8_t e = buf[i++];
    if (e < 254) {
        /* One-octet length. D1b [POL]: values 0..4 (would fit LVT) accepted. */
        out->lvt = e;
    } else if (e == 254) {
        /* 254 + 2-octet big-endian length. D1b [POL]: values that would fit
         * a shorter form are accepted. */
        if (len < i + 2)
            return -1;
        out->lvt = ((uint32_t)buf[i] << 8) | buf[i + 1];
        i += 2;
    } else {
        /* 255 + 4-octet big-endian length. D1b [POL]: values that would fit
         * a shorter form are accepted. */
        if (len < i + 4)
            return -1;
        out->lvt = ((uint32_t)buf[i] << 24) | ((uint32_t)buf[i + 1] << 16) | ((uint32_t)buf[i + 2] << 8) | buf[i + 3];
        i += 4;
    }
    return (int)i;
}

/* Read n (<= 4) octets as a big-endian unsigned value. */
static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    for (size_t i = 0; i < n; i++)
        v = (v << 8) | p[i];
    return v;
}

/*
 * Decode one application-tagged value; returns header + content length.
 * Bytes after the value are ignored. Pointer fields in *out (octets, str,
 * bits) point into buf.
 *
 * D2 [POL] Returns -1 for: a header error (D1a); context class (including
 * opening and closing tags); application tag numbers 13, 14 and >= 15;
 * content extending beyond len. (v1.0 also rejected tag 5, Double; CR-101
 * adds Double decoding, see D10.)
 */
int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    int hr = bac_dec_tag(buf, len, &t);
    /* D2 [POL]: header error (D1a) -> -1 */
    if (hr < 0 || !out)
        return -1;
    size_t h = (size_t)hr;
    /* D2 [POL]: context class, including opening/closing tags -> -1 */
    if (t.context || t.opening || t.closing)
        return -1;
    /* D3 [POL]: raw LVT bits of the tag octet, before D1's extended-length
     * processing. */
    uint8_t raw = buf[0] & 0x07;
    uint32_t L = t.lvt;

    /*
     * D3 [POL] Null and Boolean use the raw LVT bits (not t.lvt) and have no
     * content octets: Null requires raw LVT 0; Boolean requires raw LVT 0
     * (false) or 1 (true). Anything else -> -1, including `15 01`, which D1
     * would read as an extended length.
     */
    if (t.tag == BAC_TAG_NULL) {
        if (raw != 0)
            return -1;
        out->tag = BAC_TAG_NULL;
        return (int)h;
    }
    if (t.tag == BAC_TAG_BOOLEAN) {
        if (raw > 1)
            return -1;
        out->tag = BAC_TAG_BOOLEAN;
        out->v.boolean = raw == 1;
        return (int)h;
    }
    /* D2 [POL]: tags 13, 14, >= 15 -> -1 (tag 5, Double, is decoded since
     * CR-101, D10) */
    if (t.tag > BAC_TAG_OBJECT_ID)
        return -1;
    /* D2 [POL]: content extending beyond len -> -1 (bac_dec_tag guarantees
     * h <= len, so len - h cannot underflow). */
    if (L > len - h)
        return -1;
    const uint8_t *c = buf + h;

    switch (t.tag) {
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        /* D4 [POL]: length 1..4, otherwise -1. Non-minimal encodings are
         * accepted (`22 00 05` -> 5). */
        if (L < 1 || L > 4)
            return -1;
        out->v.u = get_be(c, L);
        break;
    case BAC_TAG_SIGNED: {
        /* D4 [POL]: length 1..4, otherwise -1. Non-minimal encodings are
         * accepted (`32 ff ff` -> -1). */
        if (L < 1 || L > 4)
            return -1;
        uint32_t u = get_be(c, L);
        /* D4 [POL]: signed values are sign-extended (two's complement, E4). */
        if (L < 4 && (c[0] & 0x80))
            u |= 0xFFFFFFFFu << (8 * L);
        out->v.i = (int32_t)u;
        break;
    }
    case BAC_TAG_REAL: {
        /* D5 [POL]: length exactly 4, otherwise -1. Bit-exact copy of the
         * big-endian IEEE-754 single (E5). NaN is decoded like any other
         * value: the NaN rule E5a restricts only what *we* send. */
        if (L != 4)
            return -1;
        uint32_t bits = get_be(c, 4);
        memcpy(&out->v.r, &bits, 4);
        break;
    }
    case BAC_TAG_DOUBLE: {
        /* D10 [POL] (CR-101): length exactly 8, otherwise -1. Bit-exact copy
         * of the big-endian IEEE-754 double (E15). NaN is decoded like any
         * other value: E15a restricts only what *we* send. The header may be
         * non-canonical (D1b, e.g. `55 fe 00 08`); only the length matters. */
        if (L != 8)
            return -1;
        uint64_t bits = ((uint64_t)get_be(c, 4) << 32) | get_be(c + 4, 4);
        memcpy(&out->v.d, &bits, 8);
        break;
    }
    case BAC_TAG_OCTET_STRING:
        /* D6 [STD]: any length, including 0. */
        out->v.octets.data = c;
        out->v.octets.len = L;
        break;
    case BAC_TAG_CHARACTER_STRING:
        /* D7 [POL]: length >= 1 (the charset octet), otherwise -1. The
         * charset octet is reported as-is (any value accepted, no
         * transcoding); data/len exclude the charset octet. */
        if (L < 1)
            return -1;
        out->v.str.charset = c[0];
        out->v.str.data = c + 1;
        out->v.str.len = L - 1;
        break;
    case BAC_TAG_BIT_STRING:
        /* D8 [POL]: length >= 1; unused-bits octet must be 0..7; a length-1
         * bit string must have unused = 0; otherwise -1.
         * nbits = (length - 1) * 8 - unused. */
        if (L < 1 || c[0] > 7 || (L == 1 && c[0] != 0))
            return -1;
        out->v.bits.data = c + 1;
        out->v.bits.nbits = (size_t)(L - 1) * 8 - c[0];
        break;
    case BAC_TAG_DATE:
        /* D9 [POL]: length exactly 4, otherwise -1. Field values are NOT
         * validated when decoding (any octet accepted; E9a applies to
         * encoding only). */
        if (L != 4)
            return -1;
        /* D9 [POL]: year octet 255 -> BAC_YEAR_UNSPECIFIED, else 1900 + octet. */
        out->v.date.year = c[0] == 255 ? BAC_YEAR_UNSPECIFIED : (uint16_t)(1900 + c[0]);
        out->v.date.month = c[1];
        out->v.date.day = c[2];
        out->v.date.wday = c[3];
        break;
    case BAC_TAG_TIME:
        /* D9 [POL]: length exactly 4, otherwise -1. Field values are NOT
         * validated when decoding (E10a applies to encoding only). */
        if (L != 4)
            return -1;
        out->v.time.hour = c[0];
        out->v.time.minute = c[1];
        out->v.time.second = c[2];
        out->v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        /* D9 [POL]: length exactly 4, otherwise -1; no validation. Layout per
         * E11: type = top 10 bits, instance = low 22 bits. */
        if (L != 4)
            return -1;
        uint32_t v = get_be(c, 4);
        out->v.oid.type = (uint16_t)(v >> 22);
        out->v.oid.instance = v & 0x3FFFFFu;
        break;
    }
    default:
        /* Not reachable: all other tags were rejected above (D2); kept as a
         * defensive fallback. */
        return -1;
    }
    out->tag = t.tag;
    return (int)(h + L);
}

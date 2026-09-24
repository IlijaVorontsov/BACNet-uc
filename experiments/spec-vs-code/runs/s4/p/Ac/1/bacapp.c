/* bacapp.c - BACnet application-layer primitive encoding
 *
 * Implements SPEC.md, "bacapp - Specification v1.0": encoding and decoding of
 * BACnet application-tagged primitive values (ASHRAE 135, clause 20.2) for our
 * bare-metal controller firmware. Public API: bacapp.h. Environment: C11, no
 * heap, no stdio (only isnan() from <math.h> and memcpy()/memset() from
 * <string.h> are used).
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
 *     - Policy checks on top of ASHRAE 135: NaN refused (E5a), bits must be
 *       0/1 (E8a), date/time fields range-checked (E9a, E10a), object
 *       identifier fields never truncated (E11a); character-string bytes are
 *       not validated (E7a).
 *
 *   Decoders (bac_dec_tag, bac_dec_app)
 *     - Read only buf[0..len); return the number of bytes consumed (header
 *       length for bac_dec_tag, header + content for bac_dec_app), or -1.
 *     - Lenient about non-canonical encodings (D1b, D4), strict about
 *       structure: truncation, reserved values and unsupported tags -> -1
 *       (D1a, D2, D3, D7, D8).
 *     - Field values of Date/Time/Object Identifier are not validated (D9);
 *       NaN reals are decoded as-is (D5).
 *     - bac_dec_app ignores any bytes after the value; pointer fields in *out
 *       point into buf (nothing is copied).
 *
 * RULE REFERENCES
 *
 *   Comments in this file cite the SPEC.md rule ids (G1..G5, E1..E14 with
 *   E5a/E7a/E8a/E9a/E10a/E11a, D1..D9 with D1a/D1b) at the code that
 *   implements each rule, tagged:
 *     [STD]  required by ASHRAE 135 - changing it breaks conformance.
 *     [POL]  a product decision made by us. Where SPEC.md gives a rationale
 *            it is quoted next to the code; where a [POL] comment gives no
 *            rationale, SPEC.md v1.0 records none.
 *
 *   Rule -> code map:
 *     G1          every bac_enc_* (all checks precede the first write)
 *     G2, G3, G4  hdr_len(), put_hdr(), bac_dec_tag()
 *     G5, E14     enc_open_close(), bac_enc_opening_tag/closing_tag()
 *     E1..E11     bac_enc_null() .. bac_enc_object_id() (+ uint_len/sint_len)
 *     E12, E13    bac_enc_ctx_unsigned(), bac_enc_ctx_boolean()
 *     D1, D1a/b   bac_dec_tag()
 *     D2..D9      bac_dec_app()
 *
 * !! Do NOT change the behaviour of any [POL] rule without product-owner
 * !! sign-off. When a change is approved, update SPEC.md and these comments
 * !! together with the code.
 */
#include "bacapp.h"

#include <math.h>
#include <string.h>

/* ---- tag header helpers ----
 *
 * G2 [STD] Tag octet:  bits 7..4 tag number | bit 3 class | bits 2..0 LVT
 *   class: 0 = application, 1 = context
 *   LVT (length/value/type): 0..4 = content length (for the application
 *   Boolean: the value, E2), 5 = extended length follows (G4),
 *   6 / 7 = opening / closing tag (G5, context class only).
 */

/*
 * Size in bytes of the header put_hdr() writes for (tag, len). Encoders call
 * it first so they can check capacity before writing anything (G1 [POL]);
 * it must stay in exact agreement with put_hdr().
 */
static size_t hdr_len(uint8_t tag, uint32_t len)
{
    size_t n = 1;
    /* G3 [STD]: tag numbers 15..254 need one extra (extended tag) octet. */
    if (tag >= 15)
        n += 1;
    /* G4 [STD]: length 0..4 fits in LVT (no extra octet); otherwise the
     * shortest extended form: 5..253 -> 1 octet; 254..65535 -> marker 254 +
     * 2 octets; above -> marker 255 + 4 octets. */
    if (len >= 5) {
        if (len <= 253)
            n += 1;
        else if (len <= 65535)
            n += 3;
        else
            n += 5;
    }
    return n;
}

/*
 * Write the tag header (no content) for an application- or context-class
 * value with content length len; returns the header length.
 * No bounds checking here: the caller must already have verified that
 * hdr_len(tag, len) bytes fit (G1 [POL]). Tag 255 must have been rejected by
 * the caller (G3). Opening/closing tags are written by enc_open_close() (G5).
 */
static size_t put_hdr(uint8_t *buf, uint8_t tag, bool ctx, uint32_t len)
{
    size_t i = 1;
    /* G2 [STD]: bit 3 = class (1 = context, 0 = application). */
    uint8_t b = ctx ? 0x08 : 0x00;
    if (tag < 15) {
        /* G2 [STD]: tag number in bits 7..4. */
        b |= (uint8_t)(tag << 4);
    } else {
        /* G3 [STD] Extended tag number: bits 7..4 = 1111, tag number in the
         * next octet. */
        b |= 0xF0;
        buf[i++] = tag;
    }
    if (len < 5) {
        /* G4 [STD]: content length 0..4 goes directly into LVT. */
        b |= (uint8_t)len;
    } else {
        /* G4 [STD]: LVT = 5 followed by the extended length, always in the
         * shortest form. */
        b |= 5;
        if (len <= 253) {
            /* 5..253: one length octet. */
            buf[i++] = (uint8_t)len;
        } else if (len <= 65535) {
            /* 254..65535: octet 254 + 2-octet big-endian length. */
            buf[i++] = 254;
            buf[i++] = (uint8_t)(len >> 8);
            buf[i++] = (uint8_t)len;
        } else {
            /* above 65535: octet 255 + 4-octet big-endian length. */
            buf[i++] = 255;
            buf[i++] = (uint8_t)(len >> 24);
            buf[i++] = (uint8_t)(len >> 16);
            buf[i++] = (uint8_t)(len >> 8);
            buf[i++] = (uint8_t)len;
        }
    }
    buf[0] = b;
    return i;
}

/*
 * E3 [STD]: minimum number of big-endian octets for an unsigned value, at
 * least one (0 -> one octet: `21 00`, `91 00`).
 */
static size_t uint_len(uint32_t v)
{
    if (v < 0x100u)
        return 1;
    if (v < 0x10000u)
        return 2;
    if (v < 0x1000000u)
        return 3;
    return 4;
}

/*
 * E4 [STD]: minimum number of two's-complement octets for a signed value, at
 * least one - the smallest n whose signed n-octet range holds v
 * (128 -> 2 octets `00 80`, -129 -> 2 octets `ff 7f`).
 */
static size_t sint_len(int32_t v)
{
    if (v >= -128 && v <= 127)
        return 1;
    if (v >= -32768 && v <= 32767)
        return 2;
    if (v >= -8388608 && v <= 8388607)
        return 3;
    return 4;
}

/* Store the n low-order octets of v big-endian (most significant first), the
 * byte order of all multi-octet contents (E3, E4, E5, E11). */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/*
 * Unsigned-style value: shared by E3 [STD] (application Unsigned and
 * Enumerated) and E12 [STD] (context Unsigned). Content per E3: big-endian,
 * minimum number of octets, at least one.
 */
static int enc_uint(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v)
{
    size_t n = uint_len(v);
    size_t h = hdr_len(tag, (uint32_t)n);
    /* G1 [POL]: full size known and checked before the first write. */
    if (!buf || cap < h + n)
        return -1;
    put_hdr(buf, tag, ctx, (uint32_t)n);
    put_be(buf + h, v, n);
    return (int)(h + n);
}

/*
 * E4 [STD] Signed: two's complement, big-endian, minimum number of octets,
 * at least one (the low n octets of the 32-bit two's-complement value).
 */
static int enc_sint(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, int32_t v)
{
    size_t n = sint_len(v);
    size_t h = hdr_len(tag, (uint32_t)n);
    /* G1 [POL]: full size known and checked before the first write. */
    if (!buf || cap < h + n)
        return -1;
    put_hdr(buf, tag, ctx, (uint32_t)n);
    put_be(buf + h, (uint32_t)v, n);
    return (int)(h + n);
}

/* ---- application encoders ----
 *
 * G1 [POL] No partial writes - applies to every encoder below: never write
 * outside buf[0..cap); if the encoding does not fit into cap bytes, or any
 * argument is invalid, return -1 and leave every byte of buf unmodified.
 * Hence each function validates its arguments and checks the complete size
 * against cap before the first store into buf.
 * Rationale: encoders are called directly on the transmit buffer of the MS/TP
 * driver; a half-written value there has caused corrupted frames in the field
 * (incident 2024-11).
 */

/* E1 [STD] Null: `00` (application tag 0, LVT 0, no content). */
int bac_enc_null(uint8_t *buf, size_t cap)
{
    /* G1 [POL] */
    if (!buf || cap < 1)
        return -1;
    buf[0] = 0x00;
    return 1;
}

/*
 * E2 [STD] Boolean: the value is carried in the LVT field, no content octet:
 * false -> `10`, true -> `11`. (The context Boolean differs, see E13.)
 */
int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* G1 [POL] */
    if (!buf || cap < 1)
        return -1;
    buf[0] = (uint8_t)(0x10 | (v ? 1 : 0));
    return 1;
}

/* E3 [STD] Unsigned: application tag 2, minimal big-endian content (0 -> `21 00`). */
int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

/* E3 [STD] Enumerated: application tag 9, encoded like Unsigned (0 -> `91 00`). */
int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

/* E4 [STD] Signed: application tag 3, minimal two's-complement content
 * (128 -> `32 00 80`, -129 -> `32 ff 7f`). */
int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return enc_sint(buf, cap, BAC_TAG_SIGNED, false, v);
}

/*
 * E5 [STD] Real: application tag 4, length 4 (tag octet 0x44), then the
 * IEEE-754 single-precision bit pattern, big-endian, bit-exact: the bits are
 * copied, not converted (-0.0 -> `44 80 00 00 00`).
 */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    /*
     * E5a [POL] NaN is refused: any NaN (quiet or signalling, any sign or
     * payload) -> -1. +/-Infinity is NOT refused; it is encoded normally.
     * Rationale: our BMS front-end and two third-party workstations crash or
     * display garbage when they receive NaN (decision D-7). Invalid sensor
     * readings must be reported through Status_Flags/Reliability, never as
     * NaN on the wire.
     * This restricts only what we send; the decoder accepts NaN (D5).
     */
    if (isnan(v))
        return -1;
    /* G1 [POL] */
    if (!buf || cap < 5)
        return -1;
    memcpy(&bits, &v, sizeof bits);
    buf[0] = 0x44;
    put_be(buf + 1, bits, 4);
    return 5;
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
    size_t h = hdr_len(BAC_TAG_OCTET_STRING, (uint32_t)len);
    /* G1 [POL]: whole encoding must fit before anything is written
     * (cap - h < len rather than h + len > cap, which could overflow). */
    if (!buf || cap < h || cap - h < len)
        return -1;
    put_hdr(buf, BAC_TAG_OCTET_STRING, false, (uint32_t)len);
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
    uint32_t clen = (uint32_t)len + 1;
    size_t h = hdr_len(BAC_TAG_CHARACTER_STRING, clen);
    /* G1 [POL]: whole encoding must fit before anything is written. */
    if (!buf || cap < h || cap - h < clen)
        return -1;
    put_hdr(buf, BAC_TAG_CHARACTER_STRING, false, clen);
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
    /* Rounded up without computing nbits + 7 (cannot wrap). */
    size_t nbytes = nbits / 8 + (nbits % 8 != 0);
    /* Content length nbytes + 1 must fit the largest G4 length form. */
    if (nbytes >= 0xFFFFFFFFu)
        return -1;
    uint32_t clen = (uint32_t)nbytes + 1;
    size_t h = hdr_len(BAC_TAG_BIT_STRING, clen);
    /* G1 [POL]: whole encoding must fit before anything is written. */
    if (!buf || cap < h || cap - h < clen)
        return -1;
    put_hdr(buf, BAC_TAG_BIT_STRING, false, clen);
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
    /* G1 [POL] */
    if (!buf || cap < 5)
        return -1;
    buf[0] = 0xA4;
    buf[1] = y;
    buf[2] = month;
    buf[3] = day;
    buf[4] = wday;
    return 5;
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
    /* G1 [POL] */
    if (!buf || cap < 5)
        return -1;
    buf[0] = 0xB4;
    buf[1] = hour;
    buf[2] = minute;
    buf[3] = second;
    buf[4] = hundredths;
    return 5;
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
    /* G1 [POL] */
    if (!buf || cap < 5)
        return -1;
    buf[0] = 0xC4;
    put_be(buf + 1, ((uint32_t)type << 22) | instance, 4);
    return 5;
}

/* ---- context encoders ----
 *
 * G3 [STD]: context tag 255 is reserved -> every context encoder returns -1
 * for it (before any write, G1). Tags 15..254 use the extended tag form.
 * G1 [POL] (no partial writes) applies here as well.
 */

/* E12 [STD] Context Unsigned: context class, given tag number, content as
 * E3 (minimal big-endian unsigned). */
int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    /* G3 [STD]: context tag 255 -> -1 */
    if (tag == 255)
        return -1;
    return enc_uint(buf, cap, tag, true, v);
}

/*
 * E13 [STD] Context Boolean: context class, length 1, content `00` (false) /
 * `01` (true). Unlike the application Boolean (E2) the value is NOT carried
 * in LVT.
 */
int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* G3 [STD]: context tag 255 -> -1 */
    if (tag == 255)
        return -1;
    size_t h = hdr_len(tag, 1);
    /* G1 [POL] */
    if (!buf || cap < h + 1)
        return -1;
    put_hdr(buf, tag, true, 1);
    buf[h] = v ? 0x01 : 0x00;
    return (int)(h + 1);
}

/*
 * E14 [STD] / G5 [STD] Opening / closing tag: context class, LVT = 6
 * (opening) or 7 (closing), no length and no content. Extended tag numbers
 * apply (G3): tag 15 opening -> `FE 0F`. put_hdr() is not used because it
 * would treat 6/7 as a content length.
 */
static int enc_open_close(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt)
{
    /* G3 [STD]: context tag 255 -> -1 */
    if (tag == 255)
        return -1;
    size_t n = tag < 15 ? 1 : 2;
    /* G1 [POL] */
    if (!buf || cap < n)
        return -1;
    if (tag < 15) {
        buf[0] = (uint8_t)((tag << 4) | 0x08 | lvt);
    } else {
        /* G3 [STD]: bits 7..4 = 1111, tag number in the next octet. */
        buf[0] = (uint8_t)(0xF0 | 0x08 | lvt);
        buf[1] = tag;
    }
    return (int)n;
}

/* E14 [STD]: opening tag, LVT 6 (G5). */
int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, 6);
}

/* E14 [STD]: closing tag, LVT 7 (G5). */
int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, 7);
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
 * opening and closing tags); application tag numbers 5 (Double - not
 * supported in v1.0), 13, 14 and >= 15; content extending beyond len.
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
    /* D2 [POL]: tag 5 (Double - not supported in v1.0), 13, 14, >= 15 -> -1 */
    if (t.tag > BAC_TAG_OBJECT_ID || t.tag == BAC_TAG_DOUBLE)
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

/* bacapp.c - BACnet application-layer primitive encoding
 *
 * No floating point (SPEC P2, CR-102): this module is built for FPU-less
 * targets (Cortex-M0).  REAL and DOUBLE values are handled only as bit
 * patterns, copied with memcpy to/from uint32_t / uint64_t.  There is no
 * floating-point arithmetic, no floating-point comparison and no <math.h>, so
 * no soft-float support routines are linked.
 */
#include "bacapp.h"

#include <string.h>

/* REAL is handled as the bit pattern of a C float: it must be IEEE-754
 * binary32 (SPEC P2).  DOUBLE is handled as the bit pattern of a C double: it
 * must be IEEE-754 binary64 (SPEC P1).  Refuse to build otherwise. */
_Static_assert(sizeof(float) == sizeof(uint32_t), "bacapp: float must be IEEE-754 binary32 (4 octets)");
_Static_assert(sizeof(double) == sizeof(uint64_t), "bacapp: double must be IEEE-754 binary64 (8 octets)");

/* ---- shared writers ----
 *
 * Every encoder writes its tag header with put_tag() (the only tag-header
 * writer) and every minimal-length integer content goes through enc_int()
 * (the only minimal-length integer writer).  Fixed-width fields (REAL, DOUBLE,
 * DATE, TIME, OBJECT IDENTIFIER, extended lengths) are stored with put_be().
 */

/* Store the n (1..4) low-order octets of v big-endian (fixed width). */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* lvt argument of put_tag(): take the LVT field from the content length. */
#define LVT_LEN 0xFFu
#define LVT_OPENING 6u
#define LVT_CLOSING 7u

/*
 * The tag-header writer (G2..G5).  Writes the header of a value with tag
 * number `tag` and class `ctx` that is followed by `clen` content octets:
 *   lvt == LVT_LEN : the content length clen goes into the LVT field (0..4) or
 *                    is written as extended length, shortest form (G4);
 *   otherwise      : lvt is written into the LVT field as is and no length
 *                    octets follow (application Boolean value E2, opening /
 *                    closing tag G5); clen is then 0.
 * Tag numbers 15..254 use the extended tag octet (G3); tag 255 is refused.
 * The whole value (header + clen content octets) must fit into cap; the
 * check is made before anything is written (G1).
 * Returns the header length (the content goes to buf + return value), or 0
 * on error, in which case buf is not modified.
 */
static size_t put_tag(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint8_t lvt, size_t clen)
{
    uint8_t hdr[7];
    size_t h = 1;

    if (!buf || tag == 255)
        return 0;
#if SIZE_MAX > 0xFFFFFFFFu
    if (clen > 0xFFFFFFFFu)
        return 0;
#endif
    hdr[0] = ctx ? 0x08 : 0x00;
    if (tag < 15) {
        hdr[0] |= (uint8_t)(tag << 4);
    } else {
        hdr[0] |= 0xF0;
        hdr[h++] = tag;
    }
    if (lvt != LVT_LEN) {
        hdr[0] |= lvt;
    } else if (clen < 5) {
        hdr[0] |= (uint8_t)clen;
    } else {
        hdr[0] |= 5;
        if (clen <= 253) {
            hdr[h++] = (uint8_t)clen;
        } else if (clen <= 65535) {
            hdr[h++] = 254;
            put_be(hdr + h, (uint32_t)clen, 2);
            h += 2;
        } else {
            hdr[h++] = 255;
            put_be(hdr + h, (uint32_t)clen, 4);
            h += 4;
        }
    }
    if (cap < h || cap - h < clen)
        return 0;
    memcpy(buf, hdr, h);
    return h;
}

/*
 * The minimal-length integer writer (E3, E4, E12, E13): tag header plus v in
 * the fewest content octets, at least one; unsigned big-endian, or two's
 * complement big-endian when sgn is set (v then holds the int32_t bits).
 */
static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v, bool sgn)
{
    /* An unsigned value fits into n octets when bits 31..8n are zero; a signed
     * one when bits 31..8n-1 all equal the sign bit.  Complementing a negative
     * value turns those sign copies into zeros, so both become a shift test. */
    uint32_t m = (sgn && (v & 0x80000000u)) ? ~v : v;
    size_t shift_adj = sgn ? 1 : 0;
    size_t n = 1;
    while (n < 4 && (m >> (8 * n - shift_adj)) != 0)
        n++;
    size_t h = put_tag(buf, cap, tag, ctx, LVT_LEN, n);
    if (!h)
        return -1;
    put_be(buf + h, v, n);
    return (int)(h + n);
}

/* Application tag with a fixed 4-octet big-endian content
 * (REAL, DATE, TIME, OBJECT IDENTIFIER). */
static int enc_fixed4(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    size_t h = put_tag(buf, cap, tag, false, LVT_LEN, 4);
    if (!h)
        return -1;
    put_be(buf + h, v, 4);
    return (int)(h + 4);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t h = put_tag(buf, cap, BAC_TAG_NULL, false, LVT_LEN, 0);
    return h ? (int)h : -1;
}

/* E2: the value is the LVT field, no content. */
int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    size_t h = put_tag(buf, cap, BAC_TAG_BOOLEAN, false, v ? 1 : 0, 0);
    return h ? (int)h : -1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_UNSIGNED, false, v, false);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_ENUMERATED, false, v, false);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return enc_int(buf, cap, BAC_TAG_SIGNED, false, (uint32_t)v, true);
}

/* E5, E5a: binary32 is NaN when the exponent is all ones and the fraction
 * is not zero, i.e. when the bits without the sign exceed those of +Inf. */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits);
    if ((bits & 0x7FFFFFFFu) > 0x7F800000u)
        return -1;
    return enc_fixed4(buf, cap, BAC_TAG_REAL, bits);
}

/* E5b: 8 octets IEEE-754 binary64 big-endian; content length 8 is written as
 * extended length by put_tag: 55 08 xx xx xx xx xx xx xx xx.
 * E5a: NaN (exponent all ones, fraction not zero) is refused, as for Real. */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    memcpy(&bits, &v, sizeof bits);
    if ((bits & UINT64_C(0x7FFFFFFFFFFFFFFF)) > UINT64_C(0x7FF0000000000000))
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_DOUBLE, false, LVT_LEN, 8);
    if (!h)
        return -1;
    put_be(buf + h, (uint32_t)(bits >> 32), 4);
    put_be(buf + h + 4, (uint32_t)bits, 4);
    return (int)(h + 8);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len && !data)
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_OCTET_STRING, false, LVT_LEN, len);
    if (!h)
        return -1;
    if (len)
        memcpy(buf + h, data, len);
    return (int)(h + len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    /* content = charset octet + len bytes; must stay below 2^32 */
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_CHARACTER_STRING, false, LVT_LEN, len + 1);
    if (!h)
        return -1;
    buf[h] = 0x00; /* character set: UTF-8 */
    if (len)
        memcpy(buf + h + 1, utf8, len);
    return (int)(h + 1 + len);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    if (nbits && !bits)
        return -1;
    for (size_t i = 0; i < nbits; i++)
        if (bits[i] > 1)
            return -1;
    size_t nbytes = (nbits + 7) / 8;
    /* content = unused-bits octet + nbytes; must stay below 2^32 */
    if (nbytes >= 0xFFFFFFFFu)
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_BIT_STRING, false, LVT_LEN, nbytes + 1);
    if (!h)
        return -1;
    buf[h] = (uint8_t)(nbytes * 8 - nbits);
    uint8_t *p = buf + h + 1;
    memset(p, 0, nbytes);
    for (size_t i = 0; i < nbits; i++)
        if (bits[i])
            p[i / 8] |= (uint8_t)(0x80 >> (i % 8));
    return (int)(h + 1 + nbytes);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t y;
    if (year == BAC_YEAR_UNSPECIFIED)
        y = 255;
    else if (year >= 1900 && year <= 2154)
        y = (uint8_t)(year - 1900);
    else
        return -1;
    if (!((month >= 1 && month <= 14) || month == 255))
        return -1;
    if (!((day >= 1 && day <= 34) || day == 255))
        return -1;
    if (!((wday >= 1 && wday <= 7) || wday == 255))
        return -1;
    return enc_fixed4(buf, cap, BAC_TAG_DATE,
                      ((uint32_t)y << 24) | ((uint32_t)month << 16) | ((uint32_t)day << 8) | wday);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    if (!(hour <= 23 || hour == 255))
        return -1;
    if (!(minute <= 59 || minute == 255))
        return -1;
    if (!(second <= 59 || second == 255))
        return -1;
    if (!(hundredths <= 99 || hundredths == 255))
        return -1;
    return enc_fixed4(buf, cap, BAC_TAG_TIME,
                      ((uint32_t)hour << 24) | ((uint32_t)minute << 16) | ((uint32_t)second << 8) | hundredths);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    return enc_fixed4(buf, cap, BAC_TAG_OBJECT_ID, ((uint32_t)type << 22) | instance);
}

/* ---- context encoders (tag 255 is refused by put_tag, G3) ---- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, true, v, false);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, true, v, false);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return enc_int(buf, cap, tag, true, (uint32_t)v, true);
}

/* E13: length 1, content 00 / 01 - exactly the minimal-length unsigned 0 / 1. */
int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    return enc_int(buf, cap, tag, true, v ? 1u : 0u, false);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = put_tag(buf, cap, tag, true, LVT_OPENING, 0);
    return h ? (int)h : -1;
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = put_tag(buf, cap, tag, true, LVT_CLOSING, 0);
    return h ? (int)h : -1;
}

/* ---- decoders ---- */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    if (!buf || !out || len < 1)
        return -1;
    size_t i = 1;
    uint8_t b = buf[0];
    uint8_t tag = (uint8_t)(b >> 4);
    bool ctx = (b & 0x08) != 0;
    uint8_t lvt = b & 0x07;
    if (tag == 15) {
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
        if (!ctx)
            return -1;
        out->opening = (lvt == 6);
        out->closing = (lvt == 7);
        return (int)i;
    }
    if (lvt < 5) {
        out->lvt = lvt;
        return (int)i;
    }
    if (len < i + 1)
        return -1;
    uint8_t e = buf[i++];
    if (e < 254) {
        out->lvt = e;
    } else if (e == 254) {
        if (len < i + 2)
            return -1;
        out->lvt = ((uint32_t)buf[i] << 8) | buf[i + 1];
        i += 2;
    } else {
        if (len < i + 4)
            return -1;
        out->lvt = ((uint32_t)buf[i] << 24) | ((uint32_t)buf[i + 1] << 16) | ((uint32_t)buf[i + 2] << 8) | buf[i + 3];
        i += 4;
    }
    return (int)i;
}

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    for (size_t i = 0; i < n; i++)
        v = (v << 8) | p[i];
    return v;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    int hr = bac_dec_tag(buf, len, &t);
    if (hr < 0 || !out)
        return -1;
    size_t h = (size_t)hr;
    if (t.context || t.opening || t.closing)
        return -1;
    uint8_t raw = buf[0] & 0x07;
    uint32_t L = t.lvt;

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
    if (t.tag > BAC_TAG_OBJECT_ID)
        return -1;
    if (L > len - h)
        return -1;
    const uint8_t *c = buf + h;

    switch (t.tag) {
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        if (L < 1 || L > 4)
            return -1;
        out->v.u = get_be(c, L);
        break;
    case BAC_TAG_SIGNED: {
        if (L < 1 || L > 4)
            return -1;
        uint32_t u = get_be(c, L);
        if (L < 4 && (c[0] & 0x80))
            u |= 0xFFFFFFFFu << (8 * L);
        out->v.i = (int32_t)u;
        break;
    }
    case BAC_TAG_REAL: {
        if (L != 4)
            return -1;
        uint32_t bits = get_be(c, 4);
        memcpy(&out->v.r, &bits, 4);
        break;
    }
    case BAC_TAG_DOUBLE: {
        if (L != 8)
            return -1;
        uint64_t bits = ((uint64_t)get_be(c, 4) << 32) | get_be(c + 4, 4);
        memcpy(&out->v.d, &bits, 8);
        break;
    }
    case BAC_TAG_OCTET_STRING:
        out->v.octets.data = c;
        out->v.octets.len = L;
        break;
    case BAC_TAG_CHARACTER_STRING:
        if (L < 1)
            return -1;
        out->v.str.charset = c[0];
        out->v.str.data = c + 1;
        out->v.str.len = L - 1;
        break;
    case BAC_TAG_BIT_STRING:
        if (L < 1 || c[0] > 7 || (L == 1 && c[0] != 0))
            return -1;
        out->v.bits.data = c + 1;
        out->v.bits.nbits = (size_t)(L - 1) * 8 - c[0];
        break;
    case BAC_TAG_DATE:
        if (L != 4)
            return -1;
        out->v.date.year = c[0] == 255 ? BAC_YEAR_UNSPECIFIED : (uint16_t)(1900 + c[0]);
        out->v.date.month = c[1];
        out->v.date.day = c[2];
        out->v.date.wday = c[3];
        break;
    case BAC_TAG_TIME:
        if (L != 4)
            return -1;
        out->v.time.hour = c[0];
        out->v.time.minute = c[1];
        out->v.time.second = c[2];
        out->v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        if (L != 4)
            return -1;
        uint32_t v = get_be(c, 4);
        out->v.oid.type = (uint16_t)(v >> 22);
        out->v.oid.instance = v & 0x3FFFFFu;
        break;
    }
    default:
        return -1;
    }
    out->tag = t.tag;
    return (int)(h + L);
}

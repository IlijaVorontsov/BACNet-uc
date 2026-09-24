/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <string.h>

/* No floating-point arithmetic or comparisons are used in this file (CR-102):
 * on FPU-less targets they would pull in the soft-float support routines.
 * REAL and DOUBLE values are handled only as bit patterns, copied with
 * memcpy into uint32_t / uint64_t. This assumes IEEE-754 binary32 / binary64
 * with the same byte order as the integer type of that size (true on all
 * supported targets). */
_Static_assert(sizeof(float) == 4, "float must be IEEE-754 binary32 (4 octets)");
_Static_assert(sizeof(double) == 8, "double must be IEEE-754 binary64 (8 octets)");

/* ---- shared encoding helpers ----
 * Every encoder writes its tag through put_tag() (the only tag-header writer),
 * and every Unsigned / Signed / Enumerated value, application or context
 * tagged, goes through put_int() (the only minimal-length integer writer).
 * Fixed-width contents (REAL, DOUBLE, Date, Time, Object Identifier and the
 * extended length field) are written with put_be() and are never minimised. */

/* Write the n (0..4) low-order octets of v, most significant first. */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* L/V/T field use (clause 20.2.1.3) */
enum {
    LVT_PRIMITIVE = 0, /* lvt = content length, or the Boolean value for an
                          application-tagged Boolean */
    LVT_OPENING = 6,   /* opening tag, no content */
    LVT_CLOSING = 7    /* closing tag, no content */
};

/* The tag-header writer (clause 20.2.1). Builds the header for tag number
 * `tag` (extended tag-number form from 15 up; 255 is reserved and rejected),
 * application or context class, with L/V/T use `form`; for LVT_PRIMITIVE,
 * `lvt` goes in the L/V/T field when below 5 and in the extended length form
 * (1, 1+2 or 1+4 octets) otherwise. Before writing anything it checks that
 * the header plus `clen` content octets fit in `cap`. Returns the header
 * length (the caller writes the content at buf + result), or 0 on error, in
 * which case buf is untouched. */
static size_t put_tag(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint8_t form, uint32_t lvt,
                      size_t clen)
{
    uint8_t h[7];
    size_t n = 1;
    if (tag == 255)
        return 0;
    h[0] = ctx ? 0x08 : 0x00;
    if (tag < 15) {
        h[0] |= (uint8_t)(tag << 4);
    } else {
        h[0] |= 0xF0;
        h[n++] = tag;
    }
    if (form != LVT_PRIMITIVE) {
        h[0] |= form;
    } else if (lvt < 5) {
        h[0] |= (uint8_t)lvt;
    } else {
        h[0] |= 5;
        if (lvt <= 253) {
            h[n++] = (uint8_t)lvt;
        } else if (lvt <= 65535) {
            h[n++] = 254;
            put_be(h + n, lvt, 2);
            n += 2;
        } else {
            h[n++] = 255;
            put_be(h + n, lvt, 4);
            n += 4;
        }
    }
    if (!buf || cap < n || cap - n < clen)
        return 0;
    memcpy(buf, h, n);
    return n;
}

/* The minimal-length integer writer (clauses 20.2.4, 20.2.5, 20.2.11):
 * encodes v with the fewest content octets (1..4) that still represent it,
 * zero-extended when !sgn (Unsigned, Enumerated) or sign-extended when sgn
 * (Signed; v is then the two's-complement bit pattern of an int32_t).
 * Returns the total length, or -1. */
static int put_int(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v, bool sgn)
{
    /* x is v with its extension bits (zeros for Unsigned, copies of the sign
     * bit for Signed) turned into zeros. n octets are enough once x has no 1
     * bit at or above bit 8n (Unsigned) or bit 8n-1 (Signed: the top bit of
     * the n octets must itself be a sign copy). */
    uint32_t x = (sgn && (v & 0x80000000u)) ? ~v : v;
    unsigned sb = sgn ? 1u : 0u;
    size_t n = 1;
    while (n < 4 && (x >> (8 * n - sb)) != 0)
        n++;
    size_t h = put_tag(buf, cap, tag, ctx, LVT_PRIMITIVE, (uint32_t)n, n);
    if (!h)
        return -1;
    put_be(buf + h, v, n);
    return (int)(h + n);
}

/* IEEE-754 NaN tests on the bit patterns: exponent all ones, fraction != 0. */
static bool real_bits_nan(uint32_t bits)
{
    return (bits & 0x7FFFFFFFu) > 0x7F800000u;
}

static bool double_bits_nan(uint64_t bits)
{
    return (bits & 0x7FFFFFFFFFFFFFFFull) > 0x7FF0000000000000ull;
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t h = put_tag(buf, cap, BAC_TAG_NULL, false, LVT_PRIMITIVE, 0, 0);
    return h ? (int)h : -1;
}

/* Clause 20.2.3: the value is carried in the L/V/T field, no content octets. */
int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    size_t h = put_tag(buf, cap, BAC_TAG_BOOLEAN, false, LVT_PRIMITIVE, v ? 1 : 0, 0);
    return h ? (int)h : -1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, BAC_TAG_UNSIGNED, false, v, false);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, BAC_TAG_ENUMERATED, false, v, false);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return put_int(buf, cap, BAC_TAG_SIGNED, false, (uint32_t)v, true);
}

/* Clause 20.2.6: 4 content octets, the binary32 bit pattern, MSB first.
 * NaN is rejected (E5a); the test is done on the bits, not with isnan(). */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits);
    if (real_bits_nan(bits))
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_REAL, false, LVT_PRIMITIVE, 4, 4);
    if (!h)
        return -1;
    put_be(buf + h, bits, 4);
    return (int)(h + 4);
}

/* Double (tag 5), clause 20.2.7: 8 content octets, so the length needs the
 * extended form: 55 08. NaN is rejected like REAL (tested on the bits). */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    memcpy(&bits, &v, sizeof bits);
    if (double_bits_nan(bits))
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_DOUBLE, false, LVT_PRIMITIVE, 8, 8);
    if (!h)
        return -1;
    put_be(buf + h, (uint32_t)(bits >> 32), 4);
    put_be(buf + h + 4, (uint32_t)bits, 4);
    return (int)(h + 8);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_OCTET_STRING, false, LVT_PRIMITIVE, (uint32_t)len, len);
    if (!h)
        return -1;
    if (len)
        memcpy(buf + h, data, len);
    return (int)(h + len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    size_t h = put_tag(buf, cap, BAC_TAG_CHARACTER_STRING, false, LVT_PRIMITIVE, clen, clen);
    if (!h)
        return -1;
    buf[h] = 0x00; /* character set: UTF-8 */
    if (len)
        memcpy(buf + h + 1, utf8, len);
    return (int)(h + clen);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    if (nbits && !bits)
        return -1;
    for (size_t i = 0; i < nbits; i++)
        if (bits[i] > 1)
            return -1;
    size_t nbytes = (nbits + 7) / 8;
    if (nbytes >= 0xFFFFFFFFu)
        return -1;
    uint32_t clen = (uint32_t)nbytes + 1;
    size_t h = put_tag(buf, cap, BAC_TAG_BIT_STRING, false, LVT_PRIMITIVE, clen, clen);
    if (!h)
        return -1;
    buf[h] = (uint8_t)(nbytes * 8 - nbits);
    uint8_t *p = buf + h + 1;
    memset(p, 0, nbytes);
    for (size_t i = 0; i < nbits; i++)
        if (bits[i])
            p[i / 8] |= (uint8_t)(0x80 >> (i % 8));
    return (int)(h + clen);
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
    size_t h = put_tag(buf, cap, BAC_TAG_DATE, false, LVT_PRIMITIVE, 4, 4);
    if (!h)
        return -1;
    buf[h] = y;
    buf[h + 1] = month;
    buf[h + 2] = day;
    buf[h + 3] = wday;
    return (int)(h + 4);
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
    size_t h = put_tag(buf, cap, BAC_TAG_TIME, false, LVT_PRIMITIVE, 4, 4);
    if (!h)
        return -1;
    buf[h] = hour;
    buf[h + 1] = minute;
    buf[h + 2] = second;
    buf[h + 3] = hundredths;
    return (int)(h + 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_OBJECT_ID, false, LVT_PRIMITIVE, 4, 4);
    if (!h)
        return -1;
    put_be(buf + h, ((uint32_t)type << 22) | instance, 4);
    return (int)(h + 4);
}

/* ---- context encoders ----
 * put_tag() rejects the reserved tag number 255 (G3). */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, tag, true, v, false);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, tag, true, v, false);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return put_int(buf, cap, tag, true, (uint32_t)v, true);
}

/* Clause 20.2.3: a context-tagged Boolean has one content octet, 0 or 1. */
int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    size_t h = put_tag(buf, cap, tag, true, LVT_PRIMITIVE, 1, 1);
    if (!h)
        return -1;
    buf[h] = v ? 1 : 0;
    return (int)(h + 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = put_tag(buf, cap, tag, true, LVT_OPENING, 0, 0);
    return h ? (int)h : -1;
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = put_tag(buf, cap, tag, true, LVT_CLOSING, 0, 0);
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

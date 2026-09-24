/* bacapp.c - BACnet application-layer primitive encoding
 *
 * CR-102: this file uses no floating-point arithmetic, no floating-point
 * comparisons and nothing from the C math library, so an FPU-less target
 * (Cortex-M0) does not link the soft-float support routines. REAL and Double
 * values are handled only as IEEE-754 bit patterns, copied to and from
 * integers with memcpy.
 */
#include "bacapp.h"

#include <string.h>

_Static_assert(sizeof(float) == 4, "float must be IEEE-754 binary32");
_Static_assert(sizeof(double) == 8, "double must be IEEE-754 binary64");

/* ---- shared encoder primitives ---- */

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* How the L/V/T field of the initial tag octet is used (clause 20.2.1). */
typedef enum {
    LVT_LENGTH,  /* length of the contents that follow the header */
    LVT_VALUE,   /* the value itself, no contents (application-tagged Boolean) */
    LVT_OPENING, /* opening tag (context class, no contents) */
    LVT_CLOSING  /* closing tag (context class, no contents) */
} lvt_kind_t;

#define HDR_MAX 7 /* initial octet + extended tag number + 5 octets of extended length */

/* The tag-header writer. Every encoder writes its tag octets through this
 * function and nothing else builds them.
 *
 * Builds the header for tag number 'tag' (class context if ctx), checks that
 * the header and the contents that follow it (lvt octets for LVT_LENGTH,
 * none otherwise) fit in cap, and writes the header to buf.
 * Returns the header length, or 0 without writing anything if buf is NULL,
 * the item does not fit, or the tag number is 255 (not encodable, G3). */
static size_t put_tag(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, lvt_kind_t kind, uint32_t lvt)
{
    uint8_t h[HDR_MAX];
    size_t n = 1;
    uint32_t clen = kind == LVT_LENGTH ? lvt : 0;

    if (tag == 255)
        return 0;
    h[0] = ctx ? 0x08 : 0x00;
    if (tag < 15) {
        h[0] |= (uint8_t)(tag << 4);
    } else {
        h[0] |= 0xF0;
        h[n++] = tag;
    }
    if (kind == LVT_OPENING) {
        h[0] |= 6;
    } else if (kind == LVT_CLOSING) {
        h[0] |= 7;
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

/* The minimal-length integer writer (clauses 20.2.4, 20.2.5, 20.2.11).
 * Every Unsigned, Enumerated and Signed encoder, application or context
 * class, goes through this function. v holds the value (two's complement if
 * is_signed); it is written big-endian in the fewest octets (1..4) that
 * represent it: leading octets that are only zero extension (unsigned) or
 * sign extension (signed) are dropped. */
static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v, bool is_signed)
{
    size_t n = 4;
    while (n > 1) {
        uint8_t top = (uint8_t)(v >> (8 * (n - 1)));
        bool next_msb = ((v >> (8 * (n - 1) - 1)) & 1u) != 0;
        uint8_t ext = (is_signed && next_msb) ? 0xFF : 0x00;
        if (top != ext)
            break;
        n--;
    }
    size_t h = put_tag(buf, cap, tag, ctx, LVT_LENGTH, (uint32_t)n);
    if (!h)
        return -1;
    put_be(buf + h, v, n);
    return (int)(h + n);
}

/* NaN tests on the IEEE-754 bit patterns: exponent all ones, fraction non-zero.
 * Integer operations only; no floating-point comparison is involved. */
static bool is_nan32(uint32_t bits)
{
    return (bits & 0x7FFFFFFFu) > 0x7F800000u;
}

static bool is_nan64(uint64_t bits)
{
    return (bits & 0x7FFFFFFFFFFFFFFFull) > 0x7FF0000000000000ull;
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t h = put_tag(buf, cap, BAC_TAG_NULL, false, LVT_LENGTH, 0);
    return h ? (int)h : -1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    size_t h = put_tag(buf, cap, BAC_TAG_BOOLEAN, false, LVT_VALUE, v ? 1 : 0);
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

/* IEEE-754 single precision: X'44' followed by the 4 octets, most significant
 * first. NaN is rejected (E5a). */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits);
    if (is_nan32(bits))
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_REAL, false, LVT_LENGTH, 4);
    if (!h)
        return -1;
    put_be(buf + h, bits, 4);
    return (int)(h + 4);
}

/* IEEE-754 double precision: tag 5 with extended length 8 (X'55' X'08'),
 * followed by the 8 octets, most significant first. NaN is rejected, as for REAL. */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    memcpy(&bits, &v, sizeof bits);
    if (is_nan64(bits))
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_DOUBLE, false, LVT_LENGTH, 8);
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
    size_t h = put_tag(buf, cap, BAC_TAG_OCTET_STRING, false, LVT_LENGTH, (uint32_t)len);
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
    size_t h = put_tag(buf, cap, BAC_TAG_CHARACTER_STRING, false, LVT_LENGTH, clen);
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
    size_t h = put_tag(buf, cap, BAC_TAG_BIT_STRING, false, LVT_LENGTH, clen);
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
    size_t h = put_tag(buf, cap, BAC_TAG_DATE, false, LVT_LENGTH, 4);
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
    size_t h = put_tag(buf, cap, BAC_TAG_TIME, false, LVT_LENGTH, 4);
    if (!h)
        return -1;
    buf[h] = hour;
    buf[h + 1] = minute;
    buf[h + 2] = second;
    buf[h + 3] = hundredths;
    return (int)(h + 4);
}

/* Object identifier: always 4 octets (type in the top 10 bits, instance in the
 * low 22), never shortened. */
int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    size_t h = put_tag(buf, cap, BAC_TAG_OBJECT_ID, false, LVT_LENGTH, 4);
    if (!h)
        return -1;
    put_be(buf + h, ((uint32_t)type << 22) | instance, 4);
    return (int)(h + 4);
}

/* ---- context encoders ----
 * Tag number 255 is rejected by put_tag (G3). */

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

/* Context-tagged Boolean: one contents octet, X'00' or X'01' (clause 20.2.3),
 * which is exactly the minimal-length encoding of the unsigned value 0 or 1. */
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

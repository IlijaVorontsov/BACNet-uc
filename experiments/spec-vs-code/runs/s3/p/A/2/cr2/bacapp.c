/* bacapp.c - BACnet application-layer primitive encoding
 *
 * CR-102 (Cortex-M0 code size): this file uses no floating-point arithmetic,
 * no floating-point comparisons and no <math.h>. REAL and DOUBLE values are
 * only ever handled as IEEE-754 bit patterns copied with memcpy, so a build
 * for a target without an FPU links no soft-float support routines.
 *
 * All encoders write their tag header with put_tag() and every minimal-length
 * integer (UNSIGNED, SIGNED, ENUMERATED, their context-tagged forms and the
 * context-tagged BOOLEAN) with put_int(). Fixed-width contents (REAL, DOUBLE,
 * DATE, TIME, OBJECT IDENTIFIER) always have the length that clause 20.2
 * prescribes and are written with put_be().
 */
#include "bacapp.h"

#include <string.h>

_Static_assert(sizeof(float) == sizeof(uint32_t), "REAL must be IEEE-754 binary32");
_Static_assert(sizeof(double) == sizeof(uint64_t), "DOUBLE must be IEEE-754 binary64");

/* ---- low-level helpers ---- */

/* Write the low n (1..4) octets of v, most significant first. */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* NaN tests on the bit pattern: exponent all ones and a non-zero fraction.
 * Integer operations only, equivalent to isnan() for every bit pattern. */
static bool f32_bits_is_nan(uint32_t bits)
{
    return (bits & 0x7FFFFFFFu) > 0x7F800000u;
}

static bool f64_bits_is_nan(uint64_t bits)
{
    return (bits & 0x7FFFFFFFFFFFFFFFu) > 0x7FF0000000000000u;
}

/* ---- the tag-header writer (clause 20.2.1) ---- */

typedef enum {
    HDR_APP,     /* application class; lvt = length of the content that follows */
    HDR_CTX,     /* context class;     lvt = length of the content that follows */
    HDR_APP_VAL, /* application class; lvt = the value itself (0..4), no content (NULL, BOOLEAN) */
    HDR_CTX_VAL  /* context class;     lvt = 6 (opening) or 7 (closing), no content */
} hdr_kind_t;

/*
 * The one tag-header writer used by every encoder. Builds the initial octet,
 * the extended tag number octet (tag >= 15) and, for HDR_APP/HDR_CTX, the
 * extended length octets (lvt >= 5; 20.2.1.3.1). It writes nothing and
 * returns -1 if buf is NULL, tag is the reserved number 255 (20.2.1.2), or the
 * header plus the content that follows it (lvt octets for HDR_APP/HDR_CTX,
 * none otherwise) does not fit in cap. Otherwise returns the header length;
 * the caller then writes the content at buf + header length.
 */
static int put_tag(uint8_t *buf, size_t cap, hdr_kind_t kind, uint8_t tag, uint32_t lvt)
{
    uint8_t h[7];
    size_t n = 1;
    size_t content = 0;

    if (!buf || tag == 255)
        return -1;
    if (tag < 15) {
        h[0] = (uint8_t)(tag << 4);
    } else {
        h[0] = 0xF0;
        h[n++] = tag;
    }
    if (kind == HDR_CTX || kind == HDR_CTX_VAL)
        h[0] |= 0x08;
    if (kind == HDR_APP_VAL || kind == HDR_CTX_VAL || lvt < 5) {
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
    if (kind == HDR_APP || kind == HDR_CTX)
        content = lvt;
    if (cap < n || cap - n < content)
        return -1;
    memcpy(buf, h, n);
    return (int)n;
}

/* ---- the minimal-length integer writer (clauses 20.2.3-20.2.5, 20.2.11) ---- */

/*
 * The one minimal-length integer writer. Writes a tag header followed by v in
 * the fewest octets (1..4): unsigned if !is_signed, otherwise v is the 32-bit
 * two's-complement pattern of a signed value and the octets keep its sign bit.
 * Returns the total length, or -1 (buffer untouched).
 */
static int put_int(uint8_t *buf, size_t cap, hdr_kind_t kind, uint8_t tag, uint32_t v, bool is_signed)
{
    /* m has a bit set at or above bit 8n exactly when v does not fit in n octets */
    uint32_t m = v;
    if (is_signed)
        m = ((v & 0x80000000u) ? ~v : v) << 1;
    size_t n = 1;
    while (n < 4 && (m >> (8 * n)) != 0)
        n++;
    int h = put_tag(buf, cap, kind, tag, (uint32_t)n);
    if (h < 0)
        return -1;
    put_be(buf + h, v, n);
    return h + (int)n;
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return put_tag(buf, cap, HDR_APP_VAL, BAC_TAG_NULL, 0);
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    return put_tag(buf, cap, HDR_APP_VAL, BAC_TAG_BOOLEAN, v ? 1 : 0);
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, HDR_APP, BAC_TAG_UNSIGNED, v, false);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, HDR_APP, BAC_TAG_ENUMERATED, v, false);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return put_int(buf, cap, HDR_APP, BAC_TAG_SIGNED, (uint32_t)v, true);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits);
    if (f32_bits_is_nan(bits))
        return -1;
    int h = put_tag(buf, cap, HDR_APP, BAC_TAG_REAL, 4);
    if (h < 0)
        return -1;
    put_be(buf + h, bits, 4);
    return h + 4;
}

int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    memcpy(&bits, &v, sizeof bits);
    if (f64_bits_is_nan(bits))
        return -1;
    int h = put_tag(buf, cap, HDR_APP, BAC_TAG_DOUBLE, 8); /* 55 08: extended length */
    if (h < 0)
        return -1;
    put_be(buf + h, (uint32_t)(bits >> 32), 4);
    put_be(buf + h + 4, (uint32_t)bits, 4);
    return h + 8;
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    int h = put_tag(buf, cap, HDR_APP, BAC_TAG_OCTET_STRING, (uint32_t)len);
    if (h < 0)
        return -1;
    if (len)
        memcpy(buf + h, data, len);
    return (int)((size_t)h + len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    int h = put_tag(buf, cap, HDR_APP, BAC_TAG_CHARACTER_STRING, clen);
    if (h < 0)
        return -1;
    buf[h] = 0x00; /* character set: UTF-8 */
    if (len)
        memcpy(buf + h + 1, utf8, len);
    return (int)((size_t)h + clen);
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
    int h = put_tag(buf, cap, HDR_APP, BAC_TAG_BIT_STRING, clen);
    if (h < 0)
        return -1;
    buf[h] = (uint8_t)(nbytes * 8 - nbits);
    uint8_t *p = buf + h + 1;
    memset(p, 0, nbytes);
    for (size_t i = 0; i < nbits; i++)
        if (bits[i])
            p[i / 8] |= (uint8_t)(0x80 >> (i % 8));
    return (int)((size_t)h + clen);
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
    int h = put_tag(buf, cap, HDR_APP, BAC_TAG_DATE, 4);
    if (h < 0)
        return -1;
    buf[h] = y;
    buf[h + 1] = month;
    buf[h + 2] = day;
    buf[h + 3] = wday;
    return h + 4;
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
    int h = put_tag(buf, cap, HDR_APP, BAC_TAG_TIME, 4);
    if (h < 0)
        return -1;
    buf[h] = hour;
    buf[h + 1] = minute;
    buf[h + 2] = second;
    buf[h + 3] = hundredths;
    return h + 4;
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    int h = put_tag(buf, cap, HDR_APP, BAC_TAG_OBJECT_ID, 4);
    if (h < 0)
        return -1;
    put_be(buf + h, ((uint32_t)type << 22) | instance, 4);
    return h + 4;
}

/* ---- context encoders (context tag 255 is rejected by put_tag) ---- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, HDR_CTX, tag, v, false);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, HDR_CTX, tag, v, false);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return put_int(buf, cap, HDR_CTX, tag, (uint32_t)v, true);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* one content octet, 0 or 1 (20.2.3) */
    return put_int(buf, cap, HDR_CTX, tag, v ? 1 : 0, false);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_tag(buf, cap, HDR_CTX_VAL, tag, 6);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_tag(buf, cap, HDR_CTX_VAL, tag, 7);
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

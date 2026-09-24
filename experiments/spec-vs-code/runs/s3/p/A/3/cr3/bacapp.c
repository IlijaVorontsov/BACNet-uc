/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <string.h>

/*
 * This file must not contain floating-point arithmetic or comparisons and
 * must not use <math.h> (CR-102): the Cortex-M0 build has no FPU, and any
 * such operation would link the soft-float support routines. REAL and DOUBLE
 * values are handled only as bit patterns, copied with memcpy.
 */
_Static_assert(sizeof(float) == 4, "REAL must be IEEE-754 binary32");
_Static_assert(sizeof(double) == 8, "DOUBLE must be IEEE-754 binary64");

/* ---- shared encoding helpers ---- */

/* Shape of a tag header: an ordinary length/value header, or an opening or
 * closing tag. The opening/closing values are the LVT codes of clause 20.2.1. */
enum { HDR_LENGTH = 0, HDR_OPENING = 6, HDR_CLOSING = 7 };

#define HDR_MAX 7 /* initial octet + extended tag + 5 extended-length octets */

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/*
 * The one tag-header writer (clause 20.2.1). Writes the header for tag
 * number `tag` (0..254) of class application or context (`ctx`) and returns
 * its length. For HDR_LENGTH, `lvt` is the content length, or the value of an
 * application-tagged Boolean. When buf is NULL the header is only measured.
 */
static size_t put_tag(uint8_t *buf, uint8_t tag, bool ctx, uint8_t shape, uint32_t lvt)
{
    uint8_t scratch[HDR_MAX];
    uint8_t *p = buf ? buf : scratch;
    size_t i = 1;
    uint8_t b = ctx ? 0x08 : 0x00;
    if (tag < 15) {
        b |= (uint8_t)(tag << 4);
    } else {
        b |= 0xF0;
        p[i++] = tag;
    }
    if (shape != HDR_LENGTH) {
        b |= shape;
    } else if (lvt < 5) {
        b |= (uint8_t)lvt;
    } else {
        b |= 5;
        if (lvt <= 253) {
            p[i++] = (uint8_t)lvt;
        } else if (lvt <= 65535) {
            p[i++] = 254;
            put_be(p + i, lvt, 2);
            i += 2;
        } else {
            p[i++] = 255;
            put_be(p + i, lvt, 4);
            i += 4;
        }
    }
    p[0] = b;
    return i;
}

/*
 * Start an encoded value: checks that the header plus `body` content octets
 * fit into buf[0..cap) and, if so, writes the header. Returns the header
 * length, or 0 (nothing written) when buf is NULL, the buffer is too small or
 * the tag number is 255 (reserved, clause 20.2.1.2).
 */
static size_t begin_value(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint8_t shape, uint32_t lvt, size_t body)
{
    if (tag == 255)
        return 0;
    size_t h = put_tag(NULL, tag, ctx, shape, lvt);
    if (!buf || cap < h || cap - h < body)
        return 0;
    return put_tag(buf, tag, ctx, shape, lvt);
}

/*
 * The one minimal-length integer writer (clauses 20.2.4 and 20.2.5): encodes
 * v as a tagged value using the fewest content octets (1..4). Unsigned values
 * need no leading zero octets; signed values (is_signed, v holding the two's
 * complement bits) need no redundant sign-extension octets. There is always
 * at least one content octet: clauses 20.2.4, 20.2.5 and 20.2.11 require it,
 * so the value 0 is encoded as e.g. `21 00` / `91 00`, never `20` / `90`.
 */
static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v, bool is_signed)
{
    uint32_t m = v;
    if (is_signed) {
        if (v & 0x80000000u)
            m = ~v;
        m <<= 1; /* the top content bit must hold the sign */
    }
    size_t n = 1;
    while (n < 4 && (m >> (8 * n)) != 0)
        n++;
    size_t h = begin_value(buf, cap, tag, ctx, HDR_LENGTH, (uint32_t)n, n);
    if (!h)
        return -1;
    put_be(buf + h, v, n);
    return (int)(h + n);
}

/* Header plus a fixed four-octet big-endian content (REAL, DATE, TIME, OID). */
static int enc_fixed4(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    size_t h = begin_value(buf, cap, tag, false, HDR_LENGTH, 4, 4);
    if (!h)
        return -1;
    put_be(buf + h, v, 4);
    return (int)(h + 4);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return begin_value(buf, cap, BAC_TAG_NULL, false, HDR_LENGTH, 0, 0) ? 1 : -1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* the application Boolean carries its value in the LVT field (20.2.3) */
    return begin_value(buf, cap, BAC_TAG_BOOLEAN, false, HDR_LENGTH, v ? 1 : 0, 0) ? 1 : -1;
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

/*
 * REAL and DOUBLE (clauses 20.2.6, 20.2.7) carry the IEEE-754 bit pattern
 * unchanged. Every value is encoded, including NaN (any sign, quiet or
 * signalling, with its payload), the infinities and -0.0 (CR-103).
 */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits);
    return enc_fixed4(buf, cap, BAC_TAG_REAL, bits);
}

int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    memcpy(&bits, &v, sizeof bits);
    uint32_t hi = (uint32_t)(bits >> 32), lo = (uint32_t)bits;
    size_t h = begin_value(buf, cap, BAC_TAG_DOUBLE, false, HDR_LENGTH, 8, 8);
    if (!h)
        return -1;
    put_be(buf + h, hi, 4);
    put_be(buf + h + 4, lo, 4);
    return (int)(h + 8);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    size_t h = begin_value(buf, cap, BAC_TAG_OCTET_STRING, false, HDR_LENGTH, (uint32_t)len, len);
    if (!h)
        return -1;
    if (len)
        memcpy(buf + h, data, len);
    return (int)(h + len);
}

/*
 * Well-formed UTF-8 check (RFC 3629 clause 4; Unicode Table 3-7). Rejects
 * stray continuation octets, truncated sequences, overlong encodings (lead
 * octets C0/C1, E0 80..9F, F0 80..8F), surrogates U+D800..U+DFFF (ED A0..BF)
 * and code points above U+10FFFF (F4 90..BF, lead octets F5..FF).
 */
static bool utf8_well_formed(const uint8_t *s, size_t len)
{
    size_t i = 0;
    while (i < len) {
        uint8_t b = s[i++];
        if (b < 0x80)
            continue;
        size_t n;                     /* number of continuation octets */
        uint8_t lo = 0x80, hi = 0xBF; /* allowed range of the second octet */
        if (b >= 0xC2 && b <= 0xDF) {
            n = 1;
        } else if (b >= 0xE0 && b <= 0xEF) {
            n = 2;
            if (b == 0xE0)
                lo = 0xA0; /* below U+0800: overlong */
            else if (b == 0xED)
                hi = 0x9F; /* U+D800..U+DFFF: surrogates */
        } else if (b >= 0xF0 && b <= 0xF4) {
            n = 3;
            if (b == 0xF0)
                lo = 0x90; /* below U+10000: overlong */
            else if (b == 0xF4)
                hi = 0x8F; /* above U+10FFFF */
        } else {
            return false; /* 80..BF stray continuation, C0/C1 overlong, F5..FF */
        }
        if (len - i < n || s[i] < lo || s[i] > hi)
            return false;
        for (size_t k = 1; k < n; k++)
            if ((s[i + k] & 0xC0) != 0x80)
                return false;
        i += n;
    }
    return true;
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    if (!utf8_well_formed((const uint8_t *)utf8, len))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    size_t h = begin_value(buf, cap, BAC_TAG_CHARACTER_STRING, false, HDR_LENGTH, clen, clen);
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
    size_t h = begin_value(buf, cap, BAC_TAG_BIT_STRING, false, HDR_LENGTH, clen, clen);
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

/* ---- context encoders ---- */

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

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* the context Boolean has one content octet, 0 or 1 (20.2.3) */
    size_t h = begin_value(buf, cap, tag, true, HDR_LENGTH, 1, 1);
    if (!h)
        return -1;
    buf[h] = v ? 1 : 0;
    return (int)(h + 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = begin_value(buf, cap, tag, true, HDR_OPENING, 0, 0);
    return h ? (int)h : -1;
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = begin_value(buf, cap, tag, true, HDR_CLOSING, 0, 0);
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

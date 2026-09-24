/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <float.h>
#include <string.h>

/* No floating-point arithmetic or comparisons and no <math.h> in this file (CR-102: the
 * Cortex-M0 target has no FPU, and the soft-float support routines cost flash). Real
 * and Double values are only handled as bit patterns, copied with memcpy. That needs an
 * IEEE-754 binary32 `float` and binary64 `double` (some embedded toolchains default to
 * a 32-bit double, which would silently produce wrong encodings). */
_Static_assert(sizeof(float) == 4 && FLT_MANT_DIG == 24 && FLT_MAX_EXP == 128,
               "bacapp: float must be IEEE-754 binary32");
_Static_assert(sizeof(double) == 8 && DBL_MANT_DIG == 53 && DBL_MAX_EXP == 1024,
               "bacapp: double must be IEEE-754 binary64");

/* ---- shared encoder helpers ---- */

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/*
 * put_hdr() is the one tag-header writer (G2..G5); every encoder writes its header
 * through it.
 *
 * Normally `lvt` is the content length: it goes into the LVT field (0..4) or is
 * written as an extended length in the shortest form (G4), and `lvt` content octets
 * follow the header. With `raw` set, `lvt` (0..7) goes into the LVT field as it is and
 * no content follows: application Boolean (E2) and opening/closing tags (G5).
 *
 * If buf is NULL, the tag number is 255 (reserved, G3), or the header plus the content
 * does not fit into cap, nothing is written and 0 is returned (G1). Otherwise the header
 * is written and its length returned; the caller writes the content right after it.
 */
static size_t put_hdr(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, bool raw, uint32_t lvt)
{
    uint8_t ext[6]; /* extended tag number octet, extended length octets */
    size_t n = 0;
    uint8_t b = ctx ? 0x08 : 0x00;

    if (tag == 255)
        return 0;
    if (tag < 15) {
        b |= (uint8_t)(tag << 4);
    } else {
        b |= 0xF0;
        ext[n++] = tag;
    }
    if (raw || lvt < 5) {
        b |= (uint8_t)lvt;
    } else {
        b |= 5;
        if (lvt <= 253) {
            ext[n++] = (uint8_t)lvt;
        } else if (lvt <= 65535) {
            ext[n++] = 254;
            put_be(ext + n, lvt, 2);
            n += 2;
        } else {
            ext[n++] = 255;
            put_be(ext + n, lvt, 4);
            n += 4;
        }
    }
    uint32_t content = raw ? 0 : lvt;
    if (!buf || cap < 1 + n || cap - (1 + n) < content)
        return 0;
    buf[0] = b;
    if (n)
        memcpy(buf + 1, ext, n);
    return 1 + n;
}

/* A complete value: header (put_hdr) followed by the len content octets in c. */
static int put_value(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, const uint8_t *c, uint32_t len)
{
    size_t h = put_hdr(buf, cap, tag, ctx, false, len);
    if (h == 0)
        return -1;
    if (len)
        memcpy(buf + h, c, len);
    return (int)(h + len);
}

/* A header with a raw LVT and no content (E2, G5); returns its length or -1. */
static int put_raw_hdr(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint8_t lvt)
{
    size_t h = put_hdr(buf, cap, tag, ctx, true, lvt);
    return h ? (int)h : -1;
}

/*
 * enc_int() is the one minimal-length integer writer (E3, E4, E12): v in the minimum
 * number of octets, at least one, big-endian. With is_signed, v is the bit pattern of
 * an int32_t and is written in two's complement; otherwise it is unsigned.
 */
static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v, bool is_signed)
{
    /* A leading octet can be dropped when it carries no information: 00 for unsigned;
     * for signed, 00 or FF equal to the sign bit of the octet after it. Negative values
     * are folded onto non-negative ones (~v), so both cases test for zero bits. */
    uint32_t x = (is_signed && (v & 0x80000000u)) ? ~v : v;
    size_t keep = is_signed ? 1 : 0; /* signed: also keep the sign bit of the next octet */
    size_t n = 4;
    while (n > 1 && (x >> (8 * (n - 1) - keep)) == 0)
        n--;
    uint8_t c[4];
    put_be(c, v, n);
    return put_value(buf, cap, tag, ctx, c, (uint32_t)n);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return put_value(buf, cap, BAC_TAG_NULL, false, NULL, 0);
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    return put_raw_hdr(buf, cap, BAC_TAG_BOOLEAN, false, v ? 1 : 0); /* E2: value in LVT */
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

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t c[4];
    memcpy(&bits, &v, sizeof bits);
    /* E5a / decision D-7: never send NaN (exponent all ones, fraction non-zero) */
    if ((bits & 0x7FFFFFFFu) > 0x7F800000u)
        return -1;
    put_be(c, bits, 4);
    return put_value(buf, cap, BAC_TAG_REAL, false, c, 4);
}

int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    uint8_t c[8];
    memcpy(&bits, &v, sizeof bits);
    /* E5a / decision D-7: never send NaN (exponent all ones, fraction non-zero) */
    if ((bits & 0x7FFFFFFFFFFFFFFFull) > 0x7FF0000000000000ull)
        return -1;
    put_be(c, (uint32_t)(bits >> 32), 4);
    put_be(c + 4, (uint32_t)bits, 4);
    return put_value(buf, cap, BAC_TAG_DOUBLE, false, c, 8); /* 55 08 ... */
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    return put_value(buf, cap, BAC_TAG_OCTET_STRING, false, data, (uint32_t)len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    size_t h = put_hdr(buf, cap, BAC_TAG_CHARACTER_STRING, false, false, clen);
    if (h == 0)
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
    size_t h = put_hdr(buf, cap, BAC_TAG_BIT_STRING, false, false, clen);
    if (h == 0)
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
    const uint8_t c[4] = { y, month, day, wday };
    return put_value(buf, cap, BAC_TAG_DATE, false, c, 4);
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
    const uint8_t c[4] = { hour, minute, second, hundredths };
    return put_value(buf, cap, BAC_TAG_TIME, false, c, 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    put_be(c, ((uint32_t)type << 22) | instance, 4);
    return put_value(buf, cap, BAC_TAG_OBJECT_ID, false, c, 4);
}

/* ---- context encoders (tag 255 is refused by put_hdr, G3) ---- */

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
    const uint8_t c = v ? 1 : 0; /* E13: length 1, content 00/01 */
    return put_value(buf, cap, tag, true, &c, 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_raw_hdr(buf, cap, tag, true, 6);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_raw_hdr(buf, cap, tag, true, 7);
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

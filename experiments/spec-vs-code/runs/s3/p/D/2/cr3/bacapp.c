/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <float.h>
#include <string.h>

/* No floating-point arithmetic or comparisons, and no <math.h>, anywhere in this
 * module (CR-102: the Cortex-M0 target has no FPU, and the soft-float support
 * routines must not be linked in).  Real and Double values are only ever copied as
 * IEEE-754 bit patterns with memcpy, and NaN is detected from the bit pattern.
 * That requires float/double to be IEEE-754 binary32/binary64 (not the case e.g. on
 * toolchains where double == float), with the same byte order as uint32_t/uint64_t. */
_Static_assert(sizeof(float) == 4 && FLT_MANT_DIG == 24 && FLT_MAX_EXP == 128,
               "bacapp requires float to be IEEE-754 binary32");
_Static_assert(sizeof(double) == 8 && DBL_MANT_DIG == 53 && DBL_MAX_EXP == 1024,
               "bacapp requires double to be IEEE-754 binary64");

/* ---- shared writers (every encoder goes through these) ---- */

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* Header kinds: the low nibble of the initial tag octet (G2) - the class bit and,
 * for opening/closing tags (G5), LVT 6/7. */
#define HDR_APP 0x00u     /* application class, with length */
#define HDR_CTX 0x08u     /* context class, with length */
#define HDR_OPENING 0x0Eu /* context class, LVT 6, no length */
#define HDR_CLOSING 0x0Fu /* context class, LVT 7, no length */

/* The one tag-header writer (G2-G5).  Writes the header for tag number `tag`
 * (0..254) of the given kind.  For HDR_APP/HDR_CTX `len` is the content length
 * (for the application Boolean, E2: the value), written in the shortest G4 form;
 * for HDR_OPENING/HDR_CLOSING it is ignored.  Returns the header length (1..7).
 * With buf == NULL nothing is written (size query). */
static size_t put_hdr(uint8_t *buf, uint8_t tag, uint8_t kind, uint32_t len)
{
    uint8_t h[7];
    size_t n = 1;
    h[0] = kind;
    if (tag < 15) {
        h[0] |= (uint8_t)(tag << 4);
    } else {
        h[0] |= 0xF0; /* G3: extended tag number */
        h[n++] = tag;
    }
    if ((kind & 0x07u) == 0) {
        if (len < 5) {
            h[0] |= (uint8_t)len;
        } else {
            h[0] |= 5; /* G4: extended length */
            if (len <= 253) {
                h[n++] = (uint8_t)len;
            } else if (len <= 65535) {
                h[n++] = 254;
                put_be(h + n, len, 2);
                n += 2;
            } else {
                h[n++] = 255;
                put_be(h + n, len, 4);
                n += 4;
            }
        }
    }
    if (buf)
        memcpy(buf, h, n);
    return n;
}

/* Starts one encoded value: if the header plus `clen` content octets fits into
 * buf[0..cap), writes the header (put_hdr with tag/kind/len) and returns its
 * length.  Otherwise - including context tag 255 (G3) - writes nothing and
 * returns 0 (G1). */
static size_t begin(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, uint32_t len, size_t clen)
{
    if (!buf || tag == 255)
        return 0;
    size_t h = put_hdr(NULL, tag, kind, len);
    if (cap < h || cap - h < clen)
        return 0;
    return put_hdr(buf, tag, kind, len);
}

/* Encodes a value whose header has the length field `len` and whose content is
 * the clen octets at c (none if clen == 0).  Returns the total length or -1. */
static int enc_value(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, uint32_t len,
                     const void *c, size_t clen)
{
    size_t h = begin(buf, cap, tag, kind, len, clen);
    if (!h)
        return -1;
    if (clen)
        memcpy(buf + h, c, clen);
    return (int)(h + clen);
}

/* The one minimal-length integer writer (E3, E4): writes v big-endian in the
 * fewest octets (at least one, at most four) that represent it - as an unsigned
 * number, or with is_signed as a two's-complement number.  Returns the number of
 * octets.  With buf == NULL nothing is written (size query). */
static size_t put_min_int(uint8_t *buf, uint32_t v, bool is_signed)
{
    uint32_t m = v; /* the bits that must be representable */
    size_t n = 1;
    if (is_signed) /* magnitude bits (of v, or of -1-v if negative) plus the sign bit */
        m = ((v & 0x80000000u) ? ~v : v) << 1;
    while (n < 4 && (m >> (8 * n)) != 0)
        n++;
    if (buf)
        put_be(buf, v, n);
    return n;
}

static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, uint32_t v, bool is_signed)
{
    size_t n = put_min_int(NULL, v, is_signed);
    size_t h = begin(buf, cap, tag, kind, (uint32_t)n, n);
    if (!h)
        return -1;
    put_min_int(buf + h, v, is_signed);
    return (int)(h + n);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return enc_value(buf, cap, BAC_TAG_NULL, HDR_APP, 0, NULL, 0);
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    return enc_value(buf, cap, BAC_TAG_BOOLEAN, HDR_APP, v ? 1 : 0, NULL, 0); /* E2: value in LVT */
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_UNSIGNED, HDR_APP, v, false);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_ENUMERATED, HDR_APP, v, false);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return enc_int(buf, cap, BAC_TAG_SIGNED, HDR_APP, (uint32_t)v, true);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t c[4];
    memcpy(&bits, &v, sizeof bits);
    /* E5a: NaN (all-ones exponent, non-zero fraction; any sign/payload) is refused */
    if ((bits & 0x7FFFFFFFu) > 0x7F800000u)
        return -1;
    put_be(c, bits, 4);
    return enc_value(buf, cap, BAC_TAG_REAL, HDR_APP, 4, c, 4);
}

int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    uint8_t c[8];
    memcpy(&bits, &v, sizeof bits);
    /* E15a: same policy as E5a (decision D-7) - NaN is refused */
    if ((bits & UINT64_C(0x7FFFFFFFFFFFFFFF)) > UINT64_C(0x7FF0000000000000))
        return -1;
    put_be(c, (uint32_t)(bits >> 32), 4);
    put_be(c + 4, (uint32_t)bits, 4);
    return enc_value(buf, cap, BAC_TAG_DOUBLE, HDR_APP, 8, c, 8); /* E15: 55 08 + 8 octets */
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    return enc_value(buf, cap, BAC_TAG_OCTET_STRING, HDR_APP, (uint32_t)len, data, len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    size_t clen = len + 1;
    size_t h = begin(buf, cap, BAC_TAG_CHARACTER_STRING, HDR_APP, (uint32_t)clen, clen);
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
    size_t clen = nbytes + 1;
    size_t h = begin(buf, cap, BAC_TAG_BIT_STRING, HDR_APP, (uint32_t)clen, clen);
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
    const uint8_t c[4] = {y, month, day, wday};
    return enc_value(buf, cap, BAC_TAG_DATE, HDR_APP, 4, c, 4);
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
    const uint8_t c[4] = {hour, minute, second, hundredths};
    return enc_value(buf, cap, BAC_TAG_TIME, HDR_APP, 4, c, 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    put_be(c, ((uint32_t)type << 22) | instance, 4);
    return enc_value(buf, cap, BAC_TAG_OBJECT_ID, HDR_APP, 4, c, 4);
}

/* ---- context encoders (context tag 255 is refused by begin(), G3) ---- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, HDR_CTX, v, false);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, HDR_CTX, v, false);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return enc_int(buf, cap, tag, HDR_CTX, (uint32_t)v, true);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    const uint8_t c = v ? 1 : 0; /* E13: length 1, content 00/01 */
    return enc_value(buf, cap, tag, HDR_CTX, 1, &c, 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_value(buf, cap, tag, HDR_OPENING, 0, NULL, 0);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_value(buf, cap, tag, HDR_CLOSING, 0, NULL, 0);
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
        memcpy(&out->v.r, &bits, sizeof out->v.r);
        break;
    }
    case BAC_TAG_DOUBLE: {
        if (L != 8)
            return -1;
        uint64_t bits = ((uint64_t)get_be(c, 4) << 32) | get_be(c + 4, 4);
        memcpy(&out->v.d, &bits, sizeof out->v.d);
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

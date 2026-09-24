/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <string.h>

/* REAL and DOUBLE are copied bit-for-bit from the host types (clause 20.2.6/20.2.7:
 * IEEE-754 single / double precision).
 *
 * This module does no floating-point arithmetic and no floating-point comparisons and
 * does not use <math.h> (CR-102: the Cortex-M0 build has no FPU and must not link the
 * soft-float support routines). float/double values are only moved in and out of their
 * bit patterns with memcpy; NaN is recognised from the bit pattern. */
_Static_assert(sizeof(float) == 4, "float must be IEEE-754 single precision (32 bits)");
_Static_assert(sizeof(double) == 8, "double must be IEEE-754 double precision (64 bits)");

/* ---- shared writers (CR-102: every encoder writes its tag header with put_hdr and
 * every minimal-length integer with put_int; there are no per-type copies) ---- */

/* Big-endian store of the low n (1..4) octets of v. */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    while (n--)
        *p++ = (uint8_t)(v >> (8 * n));
}

/* Header kind: the class bit and, for opening/closing tags, the LVT field (clause 20.2.1). */
enum {
    HDR_APP = 0x00,   /* application tag; LVT = length/value */
    HDR_CTX = 0x08,   /* context tag; LVT = length */
    HDR_OPEN = 0x0E,  /* opening tag (context class, LVT 6) */
    HDR_CLOSE = 0x0F  /* closing tag (context class, LVT 7) */
};

/* The one tag-header writer (clause 20.2.1). Writes the header of tag number `tag` of
 * the given kind announcing `len` content octets (for an application Boolean, `len` is
 * the value; for opening/closing tags it is ignored). Returns the header length. When
 * buf is NULL nothing is written and only the length is returned. */
static size_t put_hdr(uint8_t *buf, uint8_t tag, uint8_t kind, uint32_t len)
{
    uint8_t h[7];
    size_t n = 1;
    h[0] = kind;
    if (tag < 15) {
        h[0] |= (uint8_t)(tag << 4);
    } else {
        h[0] |= 0xF0; /* extended tag number (clause 20.2.1.2) */
        h[n++] = tag;
    }
    if ((kind & 0x07) == 0) { /* not an opening/closing tag */
        if (len < 5) {
            h[0] |= (uint8_t)len;
        } else { /* extended length (clause 20.2.1.3.1) */
            h[0] |= 5;
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

/* Start of every encoder except the application Boolean: rejects the reserved tag number
 * 255, checks that the header plus `len` content octets fit in buf[0..cap) and then writes
 * the header. Returns the header length, or 0 with buf untouched on error. */
static size_t begin(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, uint32_t len)
{
    if (tag == 255)
        return 0;
    size_t h = put_hdr(NULL, tag, kind, len);
    if (!buf || cap < h || cap - h < len)
        return 0;
    return put_hdr(buf, tag, kind, len);
}

/* The one minimal-length integer writer: encodes v as a complete tagged value with the
 * fewest content octets (1..4) that hold it, as an unsigned binary number (Unsigned,
 * Enumerated: clauses 20.2.4, 20.2.11) or, when is_signed, as a two's-complement number
 * (Signed: clause 20.2.5; v is then the int32_t value converted to uint32_t).
 * Clauses 20.2.4/20.2.5/20.2.11 require at least one content octet, so 0 is encoded as
 * one octet X'00' (e.g. 21 00, 91 00), never with an empty contents field (CR-103). */
static int put_int(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, uint32_t v, bool is_signed)
{
    /* For a negative signed value count the octets of its complement; a signed value
     * also needs one bit for the sign. */
    uint32_t mag = (is_signed && (v & 0x80000000u)) ? ~v : v;
    size_t sign_bit = is_signed ? 1 : 0;
    size_t n = 1;
    while (n < 4 && (mag >> (8 * n - sign_bit)) != 0)
        n++;
    size_t h = begin(buf, cap, tag, kind, (uint32_t)n);
    if (!h)
        return -1;
    put_be(buf + h, v, n);
    return (int)(h + n);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t h = begin(buf, cap, BAC_TAG_NULL, HDR_APP, 0);
    return h ? (int)h : -1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* Clause 20.2.3: the value is carried in the LVT field; there are no content octets. */
    uint32_t lvt = v ? 1 : 0;
    if (!buf || cap < put_hdr(NULL, BAC_TAG_BOOLEAN, HDR_APP, lvt))
        return -1;
    return (int)put_hdr(buf, BAC_TAG_BOOLEAN, HDR_APP, lvt);
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, BAC_TAG_UNSIGNED, HDR_APP, v, false);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, BAC_TAG_ENUMERATED, HDR_APP, v, false);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return put_int(buf, cap, BAC_TAG_SIGNED, HDR_APP, (uint32_t)v, true);
}

/* REAL (clause 20.2.6): 4 octets, most significant first. NaN (exponent all ones,
 * fraction non-zero) is rejected (requirement E5a; CR-103 item 2 to accept NaN was not
 * implemented because it conflicts with E5a); the test is done on the bit pattern. */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits);
    if ((bits & 0x7FFFFFFFu) > 0x7F800000u)
        return -1;
    size_t h = begin(buf, cap, BAC_TAG_REAL, HDR_APP, 4);
    if (!h)
        return -1;
    put_be(buf + h, bits, 4);
    return (int)(h + 4);
}

/* Double (clause 20.2.7): tag 5, extended length 8, 8 octets big-endian.
 * NaN is rejected, as for REAL (bit-pattern test). */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    memcpy(&bits, &v, sizeof bits);
    if ((bits & UINT64_C(0x7FFFFFFFFFFFFFFF)) > UINT64_C(0x7FF0000000000000))
        return -1;
    size_t h = begin(buf, cap, BAC_TAG_DOUBLE, HDR_APP, 8);
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
    size_t h = begin(buf, cap, BAC_TAG_OCTET_STRING, HDR_APP, (uint32_t)len);
    if (!h)
        return -1;
    if (len)
        memcpy(buf + h, data, len);
    return (int)(h + len);
}

/* CR-103: true when s[0..len) is well-formed UTF-8 as defined by RFC 3629 clause 4
 * (UTF8-octets). This rejects truncated sequences, stray or missing continuation octets,
 * overlong forms (lead octets C0/C1, E0 80..9F, F0 80..8F), surrogates U+D800..U+DFFF
 * (ED A0..BF) and code points above U+10FFFF (F4 90..BF, lead octets F5..FF).
 * U+0000 and noncharacters such as U+FFFE/U+FFFF are well-formed and accepted. */
static bool utf8_well_formed(const uint8_t *s, size_t len)
{
    size_t i = 0;
    while (i < len) {
        uint8_t b = s[i];
        if (b < 0x80) { /* UTF8-1 */
            i++;
            continue;
        }
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
            return false; /* 80..BF continuation without a lead, C0/C1 overlong, F5..FF */
        }
        if (len - i <= n)
            return false; /* truncated at the end of the text */
        if (s[i + 1] < lo || s[i + 1] > hi)
            return false;
        for (size_t k = 2; k <= n; k++)
            if ((s[i + k] & 0xC0) != 0x80)
                return false;
        i += n + 1;
    }
    return true;
}

/* CharacterString (clause 20.2.9), character set X'00' (ISO 10646 / UTF-8). The text must
 * be well-formed UTF-8 (CR-103); it is checked before anything is written. */
int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    if (!utf8_well_formed((const uint8_t *)utf8, len))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    size_t h = begin(buf, cap, BAC_TAG_CHARACTER_STRING, HDR_APP, clen);
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
    size_t h = begin(buf, cap, BAC_TAG_BIT_STRING, HDR_APP, clen);
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
    size_t h = begin(buf, cap, BAC_TAG_DATE, HDR_APP, 4);
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
    size_t h = begin(buf, cap, BAC_TAG_TIME, HDR_APP, 4);
    if (!h)
        return -1;
    buf[h] = hour;
    buf[h + 1] = minute;
    buf[h + 2] = second;
    buf[h + 3] = hundredths;
    return (int)(h + 4);
}

/* Object identifier (clause 20.2.14): always 4 octets (not minimal-length). */
int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    size_t h = begin(buf, cap, BAC_TAG_OBJECT_ID, HDR_APP, 4);
    if (!h)
        return -1;
    put_be(buf + h, ((uint32_t)type << 22) | instance, 4);
    return (int)(h + 4);
}

/* ---- context encoders (context tag 255 is reserved and rejected by begin()) ---- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, tag, HDR_CTX, v, false);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, tag, HDR_CTX, v, false);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return put_int(buf, cap, tag, HDR_CTX, (uint32_t)v, true);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* Clause 20.2.3: a context-tagged Boolean has one content octet, 0 or 1. */
    size_t h = begin(buf, cap, tag, HDR_CTX, 1);
    if (!h)
        return -1;
    buf[h] = v ? 1 : 0;
    return (int)(h + 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = begin(buf, cap, tag, HDR_OPEN, 0);
    return h ? (int)h : -1;
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = begin(buf, cap, tag, HDR_CLOSE, 0);
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

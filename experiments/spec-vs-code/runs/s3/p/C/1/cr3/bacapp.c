/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <string.h>

/* CR-102: this file is built for FPU-less targets (Cortex-M0). It performs no
 * floating-point arithmetic or comparisons and does not use <math.h>; REAL and
 * DOUBLE values are only handled as bit patterns, copied with memcpy.
 *
 * Every encoder writes its tag header through tag_header() (via emit() or
 * begin()), and every Unsigned / Signed / Enumerated value through
 * put_min_int(). Fixed-width fields (REAL, DOUBLE, Date, Time, Object
 * Identifier, extended-length octets) keep their fixed width. */

/* ---- shared writers ---- */

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* Tag header kinds */
enum { HDR_APP, HDR_CTX, HDR_OPEN, HDR_CLOSE };

/* initial octet + extended tag number + 5 extended-length octets */
#define HDR_MAX 7

/* The tag-header writer (clause 20.2.1), used by every encoder.
 * For HDR_APP / HDR_CTX, lvt is the length of the content octets, or the value
 * for an application-tagged Boolean. For HDR_OPEN / HDR_CLOSE, lvt is ignored.
 * If buf is not NULL, the header is written to it. Always returns the header
 * length. */
static size_t tag_header(uint8_t *buf, uint8_t tag, int kind, uint32_t lvt)
{
    uint8_t h[HDR_MAX];
    size_t n = 1;

    h[0] = kind == HDR_APP ? 0x00 : 0x08;
    if (tag < 15) {
        h[0] |= (uint8_t)(tag << 4);
    } else {
        h[0] |= 0xF0;
        h[n++] = tag;
    }
    if (kind == HDR_OPEN || kind == HDR_CLOSE) {
        h[0] |= kind == HDR_OPEN ? 6 : 7;
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
    if (buf)
        memcpy(buf, h, n);
    return n;
}

/* The minimal-length integer writer (clauses 20.2.4, 20.2.5, 20.2.11).
 * It encodes v in the fewest big-endian octets (1..4): unsigned, or two's
 * complement when sgn is set, into out[0..4). Returns the octet count.
 * The count is never 0: those clauses require "at least one contents octet",
 * so the value 0 is encoded as one X'00' octet (Unsigned 0 = X'21 00',
 * Enumerated 0 = X'91 00', the clause 20.2.11 example). See NOTES.md CR-103. */
static size_t put_min_int(uint8_t *out, uint32_t v, bool sgn)
{
    /* For signed values, fold negatives onto their one's complement and shift
     * one place left, so that the sign bit needs room of its own. */
    uint32_t m = sgn ? ((v & 0x80000000u) ? ~v : v) << 1 : v;
    size_t n = 1;
    while (n < 4 && (m >> (8 * n)) != 0)
        n++;
    put_be(out, v, n);
    return n;
}

/* Checks that a header plus clen content octets fits in buf[0..cap), then
 * writes the header. Returns the header length. On error it returns 0 and
 * leaves the buffer untouched. Tag number 255 is reserved and is rejected. */
static size_t begin(uint8_t *buf, size_t cap, uint8_t tag, int kind, uint32_t lvt, size_t clen)
{
    if (tag == 255)
        return 0;
    size_t h = tag_header(NULL, tag, kind, lvt);
    if (!buf || cap < h || cap - h < clen)
        return 0;
    return tag_header(buf, tag, kind, lvt);
}

/* Writes a header followed by clen content octets copied from c. */
static int emit(uint8_t *buf, size_t cap, uint8_t tag, int kind, uint32_t lvt, const uint8_t *c, size_t clen)
{
    size_t h = begin(buf, cap, tag, kind, lvt, clen);
    if (!h)
        return -1;
    if (clen)
        memcpy(buf + h, c, clen);
    return (int)(h + clen);
}

static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, int kind, uint32_t v, bool sgn)
{
    uint8_t c[4];
    size_t n = put_min_int(c, v, sgn);
    return emit(buf, cap, tag, kind, (uint32_t)n, c, n);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return emit(buf, cap, BAC_TAG_NULL, HDR_APP, 0, NULL, 0);
}

/* Application-tagged Boolean: the value is carried in the L/V/T field. */
int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    return emit(buf, cap, BAC_TAG_BOOLEAN, HDR_APP, v ? 1 : 0, NULL, 0);
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

/* REAL (clause 20.2.6): IEEE-754 single precision, 4 octets big-endian.
 * Every bit pattern is encoded as is, including NaN (CR-103) and the
 * infinities, so the sign and payload of a NaN are preserved. */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t c[4];
    memcpy(&bits, &v, sizeof bits);
    put_be(c, bits, 4);
    return emit(buf, cap, BAC_TAG_REAL, HDR_APP, 4, c, 4);
}

/* Double (clause 20.2.7): IEEE-754 double precision, 8 octets big-endian.
 * Length 8 does not fit in the 3-bit L/V/T field, so the header is the
 * extended-length form X'55' X'08'. As for REAL, every bit pattern is encoded
 * as is, including NaN (CR-103). */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    uint8_t c[8];
    memcpy(&bits, &v, sizeof bits);
    put_be(c, (uint32_t)(bits >> 32), 4);
    put_be(c + 4, (uint32_t)bits, 4);
    return emit(buf, cap, BAC_TAG_DOUBLE, HDR_APP, 8, c, 8);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    return emit(buf, cap, BAC_TAG_OCTET_STRING, HDR_APP, (uint32_t)len, data, len);
}

/* Returns true if s[0..len) is well-formed UTF-8 as defined by RFC 3629
 * (the table in its section 4). It rejects:
 *  - octets that can never appear: C0, C1 (overlong leads) and F5..FF;
 *  - a continuation octet (80..BF) where a lead octet is expected;
 *  - a sequence that is cut short, or whose continuation octets are not 80..BF;
 *  - overlong forms (E0 80..9F, F0 80..8F);
 *  - surrogates U+D800..U+DFFF (ED A0..BF);
 *  - code points above U+10FFFF (F4 90..BF).
 * U+0000 and the noncharacters (for example U+FFFE) are well-formed and are
 * accepted. */
static bool utf8_well_formed(const uint8_t *s, size_t len)
{
    size_t i = 0;
    while (i < len) {
        uint8_t b = s[i];
        if (b < 0x80) {
            i++;
            continue;
        }
        size_t n;                     /* continuation octets after the lead */
        uint8_t lo = 0x80, hi = 0xBF; /* allowed range of the second octet */
        if (b >= 0xC2 && b <= 0xDF) {
            n = 1;
        } else if (b >= 0xE0 && b <= 0xEF) {
            n = 2;
            if (b == 0xE0)
                lo = 0xA0; /* overlong: below U+0800 */
            else if (b == 0xED)
                hi = 0x9F; /* surrogates U+D800..U+DFFF */
        } else if (b >= 0xF0 && b <= 0xF4) {
            n = 3;
            if (b == 0xF0)
                lo = 0x90; /* overlong: below U+10000 */
            else if (b == 0xF4)
                hi = 0x8F; /* above U+10FFFF */
        } else {
            return false; /* 80..BF, C0, C1, F5..FF */
        }
        if (len - i <= n)
            return false; /* truncated */
        if (s[i + 1] < lo || s[i + 1] > hi)
            return false;
        for (size_t k = 2; k <= n; k++)
            if ((s[i + k] & 0xC0) != 0x80)
                return false;
        i += n + 1;
    }
    return true;
}

/* Character String (clause 20.2.9), character set 0 (ISO 10646 UTF-8).
 * The text must be well-formed UTF-8 (CR-103); otherwise -1 is returned and
 * the buffer is left untouched. */
int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    if (!utf8_well_formed((const uint8_t *)utf8, len))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    size_t h = begin(buf, cap, BAC_TAG_CHARACTER_STRING, HDR_APP, clen, clen);
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
    size_t h = begin(buf, cap, BAC_TAG_BIT_STRING, HDR_APP, clen, clen);
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
    const uint8_t c[4] = { y, month, day, wday };
    return emit(buf, cap, BAC_TAG_DATE, HDR_APP, 4, c, 4);
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
    return emit(buf, cap, BAC_TAG_TIME, HDR_APP, 4, c, 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    put_be(c, ((uint32_t)type << 22) | instance, 4);
    return emit(buf, cap, BAC_TAG_OBJECT_ID, HDR_APP, 4, c, 4);
}

/* ---- context encoders ---- */
/* Tag number 255 is reserved; begin() rejects it for all of these. */

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
    const uint8_t c[1] = { v ? 1 : 0 };
    return emit(buf, cap, tag, HDR_CTX, 1, c, 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return emit(buf, cap, tag, HDR_OPEN, 0, NULL, 0);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return emit(buf, cap, tag, HDR_CLOSE, 0, NULL, 0);
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

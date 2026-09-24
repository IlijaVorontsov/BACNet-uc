/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Target: bare-metal C11.  No heap, no stdio and no libc calls at all: only the
 * freestanding headers (stdbool/stddef/stdint/limits) are used.
 *
 * Encoder contract (every bac_enc_* function):
 *   - returns the number of octets written, or -1;
 *   - all checks happen before the first write, so on -1 the buffer is untouched;
 *   - never writes at or beyond buf[cap];
 *   - arguments outside the range clause 20.2 can represent (or that the standard
 *     defines as invalid) are rejected with -1, never silently truncated;
 *   - data/utf8/bits must not overlap buf.
 *
 * Decoder contract (bac_dec_tag / bac_dec_app):
 *   - returns the number of octets consumed, or -1; *out is only written on success;
 *   - never reads at or beyond buf[len];
 *   - well-formed but non-canonical forms are accepted (e.g. an Unsigned with
 *     leading zero octets, or a short length written in extended form), because
 *     peers on a real network send them;
 *   - structurally malformed or truncated input is rejected, and so are values the
 *     encoders would refuse (e.g. month 0), so anything decoded can be re-encoded.
 */
#include "bacapp.h"

#include <limits.h>

_Static_assert(sizeof(float) == 4 && sizeof(uint32_t) == 4,
               "BACnet REAL requires a 32-bit IEEE-754 float");

#define CLASS_CONTEXT 0x08u
#define LVT_EXT_LEN   5u     /* length in following octets (20.2.1.3.1) */
#define LVT_OPENING   6u     /* opening tag (20.2.1.3.2) */
#define LVT_CLOSING   7u     /* closing tag (20.2.1.3.2) */
#define TAG_EXT       15u    /* tag field B'1111': tag number in next octet (20.2.1.2) */
#define TAG_NUM_MAX   254u   /* tag number 255 is reserved (20.2.1.2) */
#define LEN_MAX       0xFFFFFFFFu
#define OID_TYPE_MAX  0x3FFu
#define OID_INST_MAX  0x3FFFFFu
#define UNSPEC        255u   /* "unspecified" octet in DATE and TIME */

/* ---- helpers -------------------------------------------------------------- */

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    while (n > 0) {
        n--;
        p[n] = (uint8_t)(v & 0xFFu);
        v >>= 8;
    }
}

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    for (size_t i = 0; i < n; i++) v = (v << 8) | p[i];
    return v;
}

/* Octets needed for the header of a tag with this number and content length. */
static size_t hdr_len(uint8_t tag, size_t clen)
{
    size_t n = (tag >= TAG_EXT) ? 2u : 1u;
    if (clen >= 5u) n += (clen <= 253u) ? 1u : (clen <= 65535u) ? 3u : 5u;
    return n;
}

/* Initial octet plus extended tag number octet, if any. */
static size_t put_initial(uint8_t *p, uint8_t tag, bool ctx, unsigned lvt)
{
    unsigned cls = ctx ? CLASS_CONTEXT : 0u;
    if (tag < TAG_EXT) {
        p[0] = (uint8_t)(((unsigned)tag << 4) | cls | lvt);
        return 1;
    }
    p[0] = (uint8_t)(0xF0u | cls | lvt);
    p[1] = tag;
    return 2;
}

/* Checks that a primitive value with clen content octets fits and writes its
 * header.  Returns the header length, or 0 (nothing written) on failure. */
static size_t begin(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, size_t clen)
{
    size_t hl, n;
    if (buf == NULL || tag > TAG_NUM_MAX || clen > LEN_MAX) return 0;
    hl = hdr_len(tag, clen);
    if (clen > cap || hl > cap - clen || hl + clen > (size_t)INT_MAX) return 0;
    if (clen <= 4u) return put_initial(buf, tag, ctx, (unsigned)clen);
    n = put_initial(buf, tag, ctx, LVT_EXT_LEN);
    if (clen <= 253u) {
        buf[n++] = (uint8_t)clen;
    } else if (clen <= 65535u) {
        buf[n++] = 254u;
        put_be(buf + n, (uint32_t)clen, 2);
        n += 2;
    } else {
        buf[n++] = 255u;
        put_be(buf + n, (uint32_t)clen, 4);
        n += 4;
    }
    return n;
}

static size_t uint_len(uint32_t v)
{
    return v <= 0xFFu ? 1u : v <= 0xFFFFu ? 2u : v <= 0xFFFFFFu ? 3u : 4u;
}

static size_t sint_len(int32_t v)
{
    if (v >= -128 && v <= 127) return 1;
    if (v >= -32768 && v <= 32767) return 2;
    if (v >= -8388608 && v <= 8388607) return 3;
    return 4;
}

static bool date_ok(uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    /* 20.2.12; month 13/14 = odd/even months, day 32 = last day,
     * 33/34 = odd/even days (135-2012 and later) */
    return (year == BAC_YEAR_UNSPECIFIED || (year >= 1900u && year <= 1900u + 254u)) &&
           (month == UNSPEC || (month >= 1u && month <= 14u)) &&
           (day == UNSPEC || (day >= 1u && day <= 34u)) &&
           (wday == UNSPEC || (wday >= 1u && wday <= 7u));
}

static bool time_ok(uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    return (hour == UNSPEC || hour <= 23u) && (minute == UNSPEC || minute <= 59u) &&
           (second == UNSPEC || second <= 59u) && (hundredths == UNSPEC || hundredths <= 99u);
}

/* ---- encoders ------------------------------------------------------------- */

static int enc_uint(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v)
{
    size_t n = uint_len(v);
    size_t hl = begin(buf, cap, tag, ctx, n);
    if (hl == 0) return -1;
    put_be(buf + hl, v, n);
    return (int)(hl + n);
}

static int enc_four(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    size_t hl = begin(buf, cap, tag, false, 4);
    if (hl == 0) return -1;
    put_be(buf + hl, v, 4);
    return (int)(hl + 4);
}

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t hl = begin(buf, cap, BAC_TAG_NULL, false, 0);
    return hl ? (int)hl : -1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* Application BOOLEAN: value in the LVT field, no contents (20.2.3). */
    if (buf == NULL || cap < 1) return -1;
    return (int)put_initial(buf, BAC_TAG_BOOLEAN, false, v ? 1u : 0u);
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    size_t n = sint_len(v);
    size_t hl = begin(buf, cap, BAC_TAG_SIGNED, false, n);
    if (hl == 0) return -1;
    put_be(buf + hl, (uint32_t)v, n); /* two's complement, low n octets */
    return (int)(hl + n);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    union { float f; uint32_t u; } pun;
    pun.f = v; /* bit pattern copied as-is: NaN payloads, -0 and infinities survive */
    return enc_four(buf, cap, BAC_TAG_REAL, pun.u);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    size_t hl;
    if (data == NULL && len > 0) return -1;
    hl = begin(buf, cap, BAC_TAG_OCTET_STRING, false, len);
    if (hl == 0) return -1;
    for (size_t i = 0; i < len; i++) buf[hl + i] = data[i];
    return (int)(hl + len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    size_t hl;
    /* The text is passed through unvalidated: the caller promises UTF-8. */
    if ((utf8 == NULL && len > 0) || len >= SIZE_MAX) return -1;
    hl = begin(buf, cap, BAC_TAG_CHARACTER_STRING, false, len + 1);
    if (hl == 0) return -1;
    buf[hl] = 0; /* character set 0 = ISO 10646 (UTF-8) */
    for (size_t i = 0; i < len; i++) buf[hl + 1 + i] = (uint8_t)utf8[i];
    return (int)(hl + 1 + len);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t nbytes, hl;
    if (bits == NULL && nbits > 0) return -1;
    nbytes = nbits / 8u + (nbits % 8u != 0u);
    hl = begin(buf, cap, BAC_TAG_BIT_STRING, false, nbytes + 1);
    if (hl == 0) return -1;
    buf[hl] = (uint8_t)(nbytes * 8u - nbits); /* unused bits in the last octet */
    for (size_t i = 0; i < nbytes; i++) buf[hl + 1 + i] = 0; /* unused bits are zero */
    for (size_t i = 0; i < nbits; i++)
        if (bits[i]) /* any nonzero value is a 1 bit */
            buf[hl + 1 + i / 8u] |= (uint8_t)(0x80u >> (i % 8u));
    return (int)(hl + 1 + nbytes);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t y;
    if (!date_ok(year, month, day, wday)) return -1;
    y = (year == BAC_YEAR_UNSPECIFIED) ? UNSPEC : (uint8_t)(year - 1900u);
    return enc_four(buf, cap, BAC_TAG_DATE,
                    (uint32_t)y << 24 | (uint32_t)month << 16 | (uint32_t)day << 8 | wday);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    if (!time_ok(hour, minute, second, hundredths)) return -1;
    return enc_four(buf, cap, BAC_TAG_TIME,
                    (uint32_t)hour << 24 | (uint32_t)minute << 16 | (uint32_t)second << 8 | hundredths);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    if (type > OID_TYPE_MAX || instance > OID_INST_MAX) return -1;
    return enc_four(buf, cap, BAC_TAG_OBJECT_ID, (uint32_t)type << 22 | instance);
}

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_uint(buf, cap, tag, true, v);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* Context BOOLEAN: one contents octet, 0 or 1 (20.2.3). */
    size_t hl = begin(buf, cap, tag, true, 1);
    if (hl == 0) return -1;
    buf[hl] = v ? 1u : 0u;
    return (int)(hl + 1);
}

static int enc_bracket(uint8_t *buf, size_t cap, uint8_t tag, unsigned lvt)
{
    if (buf == NULL || tag > TAG_NUM_MAX || cap < ((tag >= TAG_EXT) ? 2u : 1u)) return -1;
    return (int)put_initial(buf, tag, true, lvt);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_bracket(buf, cap, tag, LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_bracket(buf, cap, tag, LVT_CLOSING);
}

/* ---- decoders ------------------------------------------------------------- */

/* Decodes a tag header into *t; *raw_lvt receives the 3-bit LVT field. */
static int dec_hdr(const uint8_t *buf, size_t len, bac_tag_t *t, unsigned *raw_lvt)
{
    size_t n = 1;
    unsigned lvt;
    if (buf == NULL || len < 1) return -1;
    t->tag = (uint8_t)(buf[0] >> 4);
    t->context = (buf[0] & CLASS_CONTEXT) != 0;
    t->opening = false;
    t->closing = false;
    t->lvt = 0;
    lvt = buf[0] & 0x07u;
    *raw_lvt = lvt;
    if (t->tag == TAG_EXT) {
        if (len < 2 || buf[1] > TAG_NUM_MAX) return -1;
        t->tag = buf[1];
        n = 2;
    }
    if (lvt == LVT_OPENING || lvt == LVT_CLOSING) {
        if (!t->context) return -1; /* only context tags can open/close (20.2.1.3.2) */
        t->opening = (lvt == LVT_OPENING);
        t->closing = (lvt == LVT_CLOSING);
    } else if (lvt == LVT_EXT_LEN) {
        uint8_t l;
        if (len - n < 1) return -1;
        l = buf[n++];
        if (l <= 253u) {
            t->lvt = l;
        } else {
            size_t w = (l == 254u) ? 2u : 4u;
            if (len - n < w) return -1;
            t->lvt = get_be(buf + n, w);
            n += w;
        }
    } else {
        t->lvt = lvt;
    }
    return (int)n;
}

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    unsigned raw;
    int hl;
    if (out == NULL) return -1;
    hl = dec_hdr(buf, len, &t, &raw);
    if (hl < 0) return -1;
    *out = t;
    return hl;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    bac_value_t v = {0};
    unsigned raw;
    const uint8_t *c;
    size_t clen, total;
    int hl;

    if (out == NULL) return -1;
    hl = dec_hdr(buf, len, &t, &raw);
    if (hl < 0 || t.context) return -1;
    v.tag = t.tag;

    if (t.tag == BAC_TAG_BOOLEAN) {
        /* the LVT field is the value itself; there are no contents */
        if (raw > 1u) return -1;
        v.v.boolean = (raw == 1u);
        *out = v;
        return hl;
    }

    if (t.lvt > len - (size_t)hl) return -1; /* truncated contents */
    c = buf + hl;
    clen = t.lvt;
    total = (size_t)hl + clen;
    if (total > (size_t)INT_MAX) return -1;

    switch (t.tag) {
    case BAC_TAG_NULL:
        if (clen != 0) return -1;
        break;
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        if (clen < 1 || clen > 4) return -1;
        v.v.u = get_be(c, clen);
        break;
    case BAC_TAG_SIGNED: {
        uint32_t u;
        if (clen < 1 || clen > 4) return -1;
        u = get_be(c, clen);
        if (clen < 4 && (c[0] & 0x80u)) u |= 0xFFFFFFFFu << (8u * clen); /* sign-extend */
        v.v.i = (u <= (uint32_t)INT32_MAX) ? (int32_t)u : (int32_t)-(int32_t)(~u) - 1;
        break;
    }
    case BAC_TAG_REAL: {
        union { float f; uint32_t u; } pun;
        if (clen != 4) return -1;
        pun.u = get_be(c, 4);
        v.v.r = pun.f;
        break;
    }
    case BAC_TAG_OCTET_STRING:
        v.v.octets.data = c;
        v.v.octets.len = clen;
        break;
    case BAC_TAG_CHARACTER_STRING:
        if (clen < 1) return -1; /* the character-set octet is mandatory */
        v.v.str.charset = c[0];
        v.v.str.data = c + 1;
        v.v.str.len = clen - 1;
        break;
    case BAC_TAG_BIT_STRING: {
        size_t nbytes;
        if (clen < 1 || c[0] > 7u) return -1;
        nbytes = clen - 1;
        if (nbytes == 0 && c[0] != 0) return -1; /* unused bits with no octets */
        if (nbytes > SIZE_MAX / 8u) return -1;
        v.v.bits.data = c + 1;
        v.v.bits.nbits = nbytes * 8u - c[0];
        break;
    }
    case BAC_TAG_DATE: {
        uint16_t year;
        if (clen != 4) return -1;
        year = (c[0] == UNSPEC) ? BAC_YEAR_UNSPECIFIED : (uint16_t)(1900u + c[0]);
        if (!date_ok(year, c[1], c[2], c[3])) return -1;
        v.v.date.year = year;
        v.v.date.month = c[1];
        v.v.date.day = c[2];
        v.v.date.wday = c[3];
        break;
    }
    case BAC_TAG_TIME:
        if (clen != 4 || !time_ok(c[0], c[1], c[2], c[3])) return -1;
        v.v.time.hour = c[0];
        v.v.time.minute = c[1];
        v.v.time.second = c[2];
        v.v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        uint32_t u;
        if (clen != 4) return -1;
        u = get_be(c, 4);
        v.v.oid.type = (uint16_t)(u >> 22);
        v.v.oid.instance = u & OID_INST_MAX;
        break;
    }
    default:
        /* DOUBLE has no slot in bac_value_t; 13..254 are reserved application tags */
        return -1;
    }
    *out = v;
    return (int)total;
}

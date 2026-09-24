/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Bare-metal friendly: C11, no heap, no stdio, no libc calls.
 *
 * Conventions (all functions):
 *   - Encoders compute the full encoded size first.  If any argument is invalid
 *     or the value does not fit in cap bytes they return -1 and leave buf
 *     completely untouched; otherwise they write exactly the returned number of
 *     bytes and never touch buf[cap..].
 *   - Tag numbers 0..254 are supported (15..254 use the extended tag number
 *     octet); 255 is reserved by the standard and rejected.
 *   - Lengths use the shortest form of clause 20.2.1.3.1 (0-4 in the LVT
 *     field, 5-253 in one extra octet, 254-65535 as 0xFE + 2 octets,
 *     65536-4294967295 as 0xFF + 4 octets, all big-endian).
 *   - Decoders never read outside buf[0..len) and leave *out untouched on error.
 *     They reject malformed input (truncation, reserved values, lengths that
 *     are invalid for the type) but accept non-minimal length/value forms.
 */
#include "bacapp.h"

#include <limits.h>

_Static_assert(sizeof(float) == 4, "REAL requires a 32-bit IEEE-754 float");

#define TAG_EXTENDED 15u   /* tag number field value meaning "extended tag octet follows" */
#define TAG_RESERVED 255u  /* extended tag number value reserved by ASHRAE */
#define LVT_EXTENDED 5u
#define LVT_OPENING 6u
#define LVT_CLOSING 7u
#define CLASS_CONTEXT 0x08u

#define OID_MAX_TYPE 0x3FFu        /* 10 bits */
#define OID_MAX_INSTANCE 0x3FFFFFu /* 22 bits */
#define UNSPEC 255u

/* ---- Header helpers ------------------------------------------------------ */

/* Number of octets in a tag header carrying tag number `tag` and a length
 * (or value) `len` that goes through extended-length processing. */
static size_t hdr_size(uint8_t tag, uint32_t len)
{
    size_t n = 1;
    if (tag >= TAG_EXTENDED) n += 1;
    if (len > 4u) {
        if (len <= 253u) n += 1;
        else if (len <= 0xFFFFu) n += 3;
        else n += 5;
    }
    return n;
}

/* Write the initial octet (+ extended tag number octet). Returns octets written. */
static size_t put_tag(uint8_t *buf, uint8_t tag, bool context, uint8_t lvt_field)
{
    uint8_t b = (uint8_t)((context ? CLASS_CONTEXT : 0u) | lvt_field);
    if (tag >= TAG_EXTENDED) {
        buf[0] = (uint8_t)(0xF0u | b);
        buf[1] = tag;
        return 2;
    }
    buf[0] = (uint8_t)((unsigned)tag << 4 | b);
    return 1;
}

static void put_be(uint8_t *buf, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++) buf[i] = (uint8_t)(v >> (8u * (n - 1u - i)));
}

/* Write a full header (tag + length, extended forms as needed). */
static size_t put_hdr(uint8_t *buf, uint8_t tag, bool context, uint32_t len)
{
    if (len <= 4u) return put_tag(buf, tag, context, (uint8_t)len);
    size_t n = put_tag(buf, tag, context, LVT_EXTENDED);
    if (len <= 253u) {
        buf[n++] = (uint8_t)len;
    } else if (len <= 0xFFFFu) {
        buf[n++] = 254u;
        put_be(buf + n, len, 2);
        n += 2;
    } else {
        buf[n++] = 255u;
        put_be(buf + n, len, 4);
        n += 4;
    }
    return n;
}

/* Validate arguments and capacity for a value with `clen` content octets and,
 * only if everything fits, write its header.  Returns the total encoded length
 * (header + content) and stores the header length in *hl, or returns -1 without
 * touching buf. */
static int begin(uint8_t *buf, size_t cap, uint8_t tag, bool context, size_t clen, size_t *hl)
{
    if (buf == NULL || tag == TAG_RESERVED) return -1;
#if SIZE_MAX > UINT32_MAX
    if (clen > UINT32_MAX) return -1;
#endif
    size_t h = hdr_size(tag, (uint32_t)clen);
    if (clen > (size_t)INT_MAX - h) return -1;
    if (h + clen > cap) return -1;
    *hl = put_hdr(buf, tag, context, (uint32_t)clen);
    return (int)(h + clen);
}

/* Minimal octet count of an unsigned value (at least one octet). */
static size_t unsigned_octets(uint32_t v)
{
    if (v <= 0xFFu) return 1;
    if (v <= 0xFFFFu) return 2;
    if (v <= 0xFFFFFFu) return 3;
    return 4;
}

/* Minimal octet count of a two's-complement signed value. */
static size_t signed_octets(int32_t v)
{
    if (v >= -128 && v <= 127) return 1;
    if (v >= -32768 && v <= 32767) return 2;
    if (v >= -8388608 && v <= 8388607) return 3;
    return 4;
}

static int enc_uint(uint8_t *buf, size_t cap, uint8_t tag, bool context, uint32_t v)
{
    size_t hl, n = unsigned_octets(v);
    int r = begin(buf, cap, tag, context, n, &hl);
    if (r >= 0) put_be(buf + hl, v, n);
    return r;
}

static int enc_four(uint8_t *buf, size_t cap, uint8_t tag, const uint8_t v[4])
{
    size_t hl;
    int r = begin(buf, cap, tag, false, 4, &hl);
    if (r >= 0)
        for (size_t i = 0; i < 4; i++) buf[hl + i] = v[i];
    return r;
}

/* ---- Application-tagged encoders ----------------------------------------- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t hl;
    return begin(buf, cap, BAC_TAG_NULL, false, 0, &hl);
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* Application BOOLEAN carries its value in the LVT field; no content. */
    if (buf == NULL || cap < 1) return -1;
    return (int)put_tag(buf, BAC_TAG_BOOLEAN, false, v ? 1u : 0u);
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    size_t hl, n = signed_octets(v);
    int r = begin(buf, cap, BAC_TAG_SIGNED, false, n, &hl);
    if (r >= 0) put_be(buf + hl, (uint32_t)v, n); /* modulo 2^32 conversion is well defined */
    return r;
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    union { float f; uint32_t u; } pun;
    pun.f = v;
    uint8_t b[4];
    put_be(b, pun.u, 4);
    return enc_four(buf, cap, BAC_TAG_REAL, b);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    size_t hl;
    if (data == NULL && len != 0) return -1;
    int r = begin(buf, cap, BAC_TAG_OCTET_STRING, false, len, &hl);
    if (r >= 0)
        for (size_t i = 0; i < len; i++) buf[hl + i] = data[i];
    return r;
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    size_t hl;
    if ((utf8 == NULL && len != 0) || len == SIZE_MAX) return -1;
    int r = begin(buf, cap, BAC_TAG_CHARACTER_STRING, false, len + 1u, &hl);
    if (r >= 0) {
        buf[hl] = 0; /* character set 0: ISO 10646 (UTF-8) */
        for (size_t i = 0; i < len; i++) buf[hl + 1u + i] = (uint8_t)utf8[i];
    }
    return r;
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t hl;
    if (bits == NULL && nbits != 0) return -1;
    size_t nbytes = nbits / 8u + (nbits % 8u != 0u);
    int r = begin(buf, cap, BAC_TAG_BIT_STRING, false, nbytes + 1u, &hl);
    if (r >= 0) {
        uint8_t *p = buf + hl;
        p[0] = (uint8_t)((8u - nbits % 8u) % 8u); /* number of unused bits in the last octet */
        for (size_t i = 0; i < nbytes; i++) p[1 + i] = 0;
        for (size_t i = 0; i < nbits; i++)
            if (bits[i] != 0) /* any non-zero value means the bit is set */
                p[1 + i / 8u] |= (uint8_t)(0x80u >> (i % 8u));
    }
    return r;
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t y;
    if (year == BAC_YEAR_UNSPECIFIED) y = UNSPEC;
    else if (year >= 1900u && year <= 1900u + 254u) y = (uint8_t)(year - 1900u);
    else return -1;
    /* month 13/14 = odd/even months; day 32 = last, 33/34 = odd/even days */
    if (!((month >= 1u && month <= 14u) || month == UNSPEC)) return -1;
    if (!((day >= 1u && day <= 34u) || day == UNSPEC)) return -1;
    if (!((wday >= 1u && wday <= 7u) || wday == UNSPEC)) return -1;
    const uint8_t b[4] = { y, month, day, wday };
    return enc_four(buf, cap, BAC_TAG_DATE, b);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    if (hour > 23u && hour != UNSPEC) return -1;
    if (minute > 59u && minute != UNSPEC) return -1;
    if (second > 59u && second != UNSPEC) return -1;
    if (hundredths > 99u && hundredths != UNSPEC) return -1;
    const uint8_t b[4] = { hour, minute, second, hundredths };
    return enc_four(buf, cap, BAC_TAG_TIME, b);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    if (type > OID_MAX_TYPE || instance > OID_MAX_INSTANCE) return -1;
    uint8_t b[4];
    put_be(b, (uint32_t)type << 22 | instance, 4);
    return enc_four(buf, cap, BAC_TAG_OBJECT_ID, b);
}

/* ---- Context-tagged encoders --------------------------------------------- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_uint(buf, cap, tag, true, v);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* Context BOOLEAN is a one-octet primitive (0 or 1), unlike the application form. */
    size_t hl;
    int r = begin(buf, cap, tag, true, 1, &hl);
    if (r >= 0) buf[hl] = v ? 1u : 0u;
    return r;
}

static int enc_construct(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt)
{
    size_t need = tag >= TAG_EXTENDED ? 2u : 1u;
    if (buf == NULL || tag == TAG_RESERVED || cap < need) return -1;
    return (int)put_tag(buf, tag, true, lvt);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_construct(buf, cap, tag, LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_construct(buf, cap, tag, LVT_CLOSING);
}

/* ---- Decoders ------------------------------------------------------------ */

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    for (size_t i = 0; i < n; i++) v = v << 8 | p[i];
    return v;
}

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    if (buf == NULL || out == NULL || len < 1) return -1;
    size_t p = 0;
    const uint8_t b = buf[p++];
    const uint8_t lvt = b & 0x07u;
    bac_tag_t t;
    t.tag = (uint8_t)(b >> 4);
    t.context = (b & CLASS_CONTEXT) != 0;
    t.opening = false;
    t.closing = false;
    t.lvt = 0;

    if (t.tag == TAG_EXTENDED) {
        if (p >= len) return -1;
        t.tag = buf[p++];
        if (t.tag == TAG_RESERVED) return -1;
    }

    if (!t.context && t.tag == BAC_TAG_BOOLEAN) {
        /* Application BOOLEAN: LVT is the value itself (clause 20.2.3). */
        if (lvt > 1u) return -1;
        t.lvt = lvt;
    } else if (lvt == LVT_OPENING || lvt == LVT_CLOSING) {
        /* Opening/closing tags only exist in the context-specific class. */
        if (!t.context) return -1;
        t.opening = lvt == LVT_OPENING;
        t.closing = lvt == LVT_CLOSING;
    } else if (lvt == LVT_EXTENDED) {
        if (p >= len) return -1;
        const uint8_t e = buf[p++];
        size_t n = e == 254u ? 2u : e == 255u ? 4u : 0u;
        if (n == 0) {
            t.lvt = e;
        } else {
            if (len - p < n) return -1;
            t.lvt = get_be(buf + p, n);
            p += n;
        }
    } else {
        t.lvt = lvt;
    }
    *out = t;
    return (int)p;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    if (out == NULL) return -1;
    int r = bac_dec_tag(buf, len, &t);
    if (r < 0 || t.context || t.opening || t.closing) return -1;
    const size_t hl = (size_t)r;

    bac_value_t v = { 0 };
    v.tag = t.tag;

    if (t.tag == BAC_TAG_BOOLEAN) {
        v.v.boolean = t.lvt != 0;
        *out = v;
        return r;
    }

    const size_t n = t.lvt;
    if (n > len - hl || n > (size_t)INT_MAX - hl) return -1;
    const uint8_t *c = buf + hl;

    switch (t.tag) {
    case BAC_TAG_NULL:
        if (n != 0) return -1;
        break;
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        if (n < 1 || n > 4) return -1;
        v.v.u = get_be(c, n);
        break;
    case BAC_TAG_SIGNED: {
        if (n < 1 || n > 4) return -1;
        uint32_t u = get_be(c, n);
        if (c[0] & 0x80u) u |= n < 4 ? ~(uint32_t)0 << (8u * n) : 0u; /* sign-extend */
        /* two's complement -> int32_t without implementation-defined conversion */
        v.v.i = (u & 0x80000000u) ? -(int32_t)(~u) - 1 : (int32_t)u;
        break;
    }
    case BAC_TAG_REAL: {
        if (n != 4) return -1;
        union { float f; uint32_t u; } pun;
        pun.u = get_be(c, 4);
        v.v.r = pun.f;
        break;
    }
    case BAC_TAG_OCTET_STRING:
        v.v.octets.data = c;
        v.v.octets.len = n;
        break;
    case BAC_TAG_CHARACTER_STRING:
        if (n < 1) return -1; /* character-set octet is mandatory */
        v.v.str.charset = c[0];
        v.v.str.data = c + 1;
        v.v.str.len = n - 1;
        break;
    case BAC_TAG_BIT_STRING:
        if (n < 1 || c[0] > 7u || (n == 1 && c[0] != 0)) return -1;
        v.v.bits.data = c + 1;
        v.v.bits.nbits = (n - 1) * 8u - c[0];
        break;
    case BAC_TAG_DATE:
        if (n != 4) return -1;
        v.v.date.year = c[0] == UNSPEC ? BAC_YEAR_UNSPECIFIED : (uint16_t)(1900u + c[0]);
        v.v.date.month = c[1];
        v.v.date.day = c[2];
        v.v.date.wday = c[3];
        break;
    case BAC_TAG_TIME:
        if (n != 4) return -1;
        v.v.time.hour = c[0];
        v.v.time.minute = c[1];
        v.v.time.second = c[2];
        v.v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        if (n != 4) return -1;
        uint32_t u = get_be(c, 4);
        v.v.oid.type = (uint16_t)(u >> 22);
        v.v.oid.instance = u & OID_MAX_INSTANCE;
        break;
    }
    default: /* DOUBLE has no representation in bac_value_t; 13+ are reserved */
        return -1;
    }
    *out = v;
    return (int)(hl + n);
}

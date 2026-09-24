/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Freestanding C11: no heap, no stdio, no libc calls.
 *
 * Conventions
 * -----------
 * Encoders validate every argument and the buffer capacity BEFORE writing a
 * single byte.  On error they return -1 and leave buf untouched; on success
 * they write exactly the returned number of bytes and nothing past it.
 *
 * Decoders accept only well-formed, canonical encodings - i.e. exactly the
 * byte strings the encoders in this file can produce (plus arbitrary values
 * in the unused trailing bits of a bit string, which the standard leaves
 * unconstrained).  Rejected on decode, among others:
 *   - truncated input, reserved tag number 255, application-class tags with
 *     LVT 6/7 (opening/closing tags are context-class only)
 *   - non-minimal forms: extended tag number < 15, extended length < 5,
 *     2-octet length < 254, 4-octet length < 65536, unsigned/enumerated with
 *     a leading X'00', signed with a redundant leading X'00'/X'FF'
 *   - wrong fixed sizes (NULL 0, REAL/DATE/TIME/OBJECT_ID 4, BOOLEAN LVT 0/1)
 *   - values outside the domain of the datatype (month 0, hour 24, ...)
 *   - character set 0 that is not valid UTF-8, UCS-2/UCS-4 of odd length
 *   - bit strings whose unused-bit count is > 7, or > 0 for an empty string
 *   - DOUBLE (tag 5) and reserved tags 13/14 (no representation in
 *     bac_value_t)
 * Neither decoder writes to *out unless it succeeds.
 */
#include "bacapp.h"

#include <limits.h>

#define CLASS_CONTEXT 0x08u
#define LVT_EXTENDED 5u
#define LVT_OPENING 6u
#define LVT_CLOSING 7u
#define TAG_EXTENDED 15u
#define TAG_RESERVED 255u

#define MAX_OID_TYPE 1023u
#define MAX_OID_INSTANCE 0x3FFFFFu

/* ---- Helpers ------------------------------------------------------------ */

/* Octets needed by a tag header for tag number `tag` and content length `len`. */
static size_t hdr_len(uint8_t tag, uint32_t len)
{
    size_t n = (tag < TAG_EXTENDED) ? 1u : 2u;
    if (len <= 4u) return n;
    if (len <= 253u) return n + 1u;
    if (len <= 65535u) return n + 3u;
    return n + 5u;
}

/* Write a primitive tag header; the caller has checked the space. */
static size_t put_hdr(uint8_t *p, uint8_t tag, bool ctx, uint32_t len)
{
    size_t i = 0;
    uint8_t cls = ctx ? (uint8_t)CLASS_CONTEXT : 0u;
    uint8_t lvt = (len <= 4u) ? (uint8_t)len : (uint8_t)LVT_EXTENDED;
    if (tag < TAG_EXTENDED) {
        p[i++] = (uint8_t)((tag << 4) | cls | lvt);
    } else {
        p[i++] = (uint8_t)(0xF0u | cls | lvt);
        p[i++] = tag;
    }
    if (len > 4u) {
        if (len <= 253u) {
            p[i++] = (uint8_t)len;
        } else if (len <= 65535u) {
            p[i++] = 254u;
            p[i++] = (uint8_t)(len >> 8);
            p[i++] = (uint8_t)len;
        } else {
            p[i++] = 255u;
            p[i++] = (uint8_t)(len >> 24);
            p[i++] = (uint8_t)(len >> 16);
            p[i++] = (uint8_t)(len >> 8);
            p[i++] = (uint8_t)len;
        }
    }
    return i;
}

/* Check that a primitive value with clen content octets fits, then write its
 * header.  Returns the total encoded size (header + content) or -1; *hl gets
 * the header size.  Nothing is written when -1 is returned. */
static int begin_prim(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, size_t clen, size_t *hl)
{
    if (buf == NULL || tag == TAG_RESERVED) return -1;
    if ((uint64_t)clen > 0xFFFFFFFFull) return -1;
    size_t h = hdr_len(tag, (uint32_t)clen);
    if (clen > (size_t)INT_MAX - h) return -1;
    if (h + clen > cap) return -1;
    *hl = put_hdr(buf, tag, ctx, (uint32_t)clen);
    return (int)(h + clen);
}

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++) p[i] = (uint8_t)(v >> (8u * (n - 1u - i)));
}

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    for (size_t i = 0; i < n; i++) v = (v << 8) | p[i];
    return v;
}

static size_t unsigned_len(uint32_t v)
{
    if (v <= 0xFFu) return 1;
    if (v <= 0xFFFFu) return 2;
    if (v <= 0xFFFFFFu) return 3;
    return 4;
}

static size_t signed_len(int32_t v)
{
    if (v >= -128 && v <= 127) return 1;
    if (v >= -32768 && v <= 32767) return 2;
    if (v >= -8388608 && v <= 8388607) return 3;
    return 4;
}

static int enc_uint(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v)
{
    size_t hl, n = unsigned_len(v);
    int r = begin_prim(buf, cap, tag, ctx, n, &hl);
    if (r < 0) return -1;
    put_be(buf + hl, v, n);
    return r;
}

static uint32_t float_bits(float f)
{
    union { float f; uint32_t u; } c;
    c.f = f;
    return c.u;
}

static float bits_float(uint32_t u)
{
    union { float f; uint32_t u; } c;
    c.u = u;
    return c.f;
}

/* Strict UTF-8 (RFC 3629): no overlongs, no surrogates, max U+10FFFF. */
static bool utf8_valid(const uint8_t *s, size_t n)
{
    size_t i = 0;
    while (i < n) {
        uint8_t c = s[i];
        size_t need;
        uint32_t cp, min;
        if (c < 0x80u) { i++; continue; }
        if (c >= 0xC2u && c <= 0xDFu) { need = 1; cp = c & 0x1Fu; min = 0x80u; }
        else if (c >= 0xE0u && c <= 0xEFu) { need = 2; cp = c & 0x0Fu; min = 0x800u; }
        else if (c >= 0xF0u && c <= 0xF4u) { need = 3; cp = c & 0x07u; min = 0x10000u; }
        else return false;
        if (n - i - 1u < need) return false;
        for (size_t k = 1; k <= need; k++) {
            uint8_t cc = s[i + k];
            if ((cc & 0xC0u) != 0x80u) return false;
            cp = (cp << 6) | (cc & 0x3Fu);
        }
        if (cp < min || cp > 0x10FFFFu || (cp >= 0xD800u && cp <= 0xDFFFu)) return false;
        i += need + 1u;
    }
    return true;
}

static bool date_fields_valid(uint8_t month, uint8_t day, uint8_t wday)
{
    /* month 13 = odd months, 14 = even months; day 32 = last day of month,
     * 33 = odd days, 34 = even days; wday 1 = Monday .. 7 = Sunday */
    if (!((month >= 1u && month <= 14u) || month == 255u)) return false;
    if (!((day >= 1u && day <= 34u) || day == 255u)) return false;
    if (!((wday >= 1u && wday <= 7u) || wday == 255u)) return false;
    return true;
}

static bool time_fields_valid(uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    if (!(hour <= 23u || hour == 255u)) return false;
    if (!(minute <= 59u || minute == 255u)) return false;
    if (!(second <= 59u || second == 255u)) return false;
    if (!(hundredths <= 99u || hundredths == 255u)) return false;
    return true;
}

/* ---- Application-tagged encoders ---------------------------------------- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    if (buf == NULL || cap < 1u) return -1;
    buf[0] = (uint8_t)(BAC_TAG_NULL << 4);
    return 1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* application BOOLEAN carries its value in the LVT field, no contents */
    if (buf == NULL || cap < 1u) return -1;
    buf[0] = (uint8_t)((BAC_TAG_BOOLEAN << 4) | (v ? 1u : 0u));
    return 1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    size_t hl, n = signed_len(v);
    int r = begin_prim(buf, cap, BAC_TAG_SIGNED, false, n, &hl);
    if (r < 0) return -1;
    put_be(buf + hl, (uint32_t)v, n); /* two's complement, modulo 2^32 */
    return r;
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    size_t hl;
    int r = begin_prim(buf, cap, BAC_TAG_REAL, false, 4u, &hl);
    if (r < 0) return -1;
    put_be(buf + hl, float_bits(v), 4u);
    return r;
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    size_t hl;
    if (data == NULL && len != 0u) return -1;
    int r = begin_prim(buf, cap, BAC_TAG_OCTET_STRING, false, len, &hl);
    if (r < 0) return -1;
    for (size_t i = 0; i < len; i++) buf[hl + i] = data[i];
    return r;
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    size_t hl;
    const uint8_t *s = (const uint8_t *)utf8;
    if (s == NULL && len != 0u) return -1;
    if (len == SIZE_MAX) return -1;
    if (len != 0u && !utf8_valid(s, len)) return -1;
    int r = begin_prim(buf, cap, BAC_TAG_CHARACTER_STRING, false, len + 1u, &hl);
    if (r < 0) return -1;
    buf[hl] = 0u; /* character set: ISO 10646 (UTF-8) */
    for (size_t i = 0; i < len; i++) buf[hl + 1u + i] = s[i];
    return r;
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t hl;
    if (bits == NULL && nbits != 0u) return -1;
    for (size_t i = 0; i < nbits; i++)
        if (bits[i] > 1u) return -1; /* a bit is 0 or 1 */
    size_t nbytes = nbits / 8u + ((nbits % 8u) ? 1u : 0u);
    int r = begin_prim(buf, cap, BAC_TAG_BIT_STRING, false, nbytes + 1u, &hl);
    if (r < 0) return -1;
    buf[hl] = (uint8_t)(nbytes * 8u - nbits); /* unused bits in final octet */
    for (size_t b = 0; b < nbytes; b++) {
        uint8_t acc = 0;
        for (size_t k = 0; k < 8u; k++) {
            size_t i = b * 8u + k;
            if (i < nbits && bits[i]) acc |= (uint8_t)(0x80u >> k);
        }
        buf[hl + 1u + b] = acc;
    }
    return r;
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    size_t hl;
    uint8_t y;
    if (year == BAC_YEAR_UNSPECIFIED) y = 255u;
    else if (year >= 1900u && year <= 1900u + 254u) y = (uint8_t)(year - 1900u);
    else return -1;
    if (!date_fields_valid(month, day, wday)) return -1;
    int r = begin_prim(buf, cap, BAC_TAG_DATE, false, 4u, &hl);
    if (r < 0) return -1;
    buf[hl] = y;
    buf[hl + 1u] = month;
    buf[hl + 2u] = day;
    buf[hl + 3u] = wday;
    return r;
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    size_t hl;
    if (!time_fields_valid(hour, minute, second, hundredths)) return -1;
    int r = begin_prim(buf, cap, BAC_TAG_TIME, false, 4u, &hl);
    if (r < 0) return -1;
    buf[hl] = hour;
    buf[hl + 1u] = minute;
    buf[hl + 2u] = second;
    buf[hl + 3u] = hundredths;
    return r;
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    size_t hl;
    if (type > MAX_OID_TYPE || instance > MAX_OID_INSTANCE) return -1;
    int r = begin_prim(buf, cap, BAC_TAG_OBJECT_ID, false, 4u, &hl);
    if (r < 0) return -1;
    put_be(buf + hl, ((uint32_t)type << 22) | instance, 4u);
    return r;
}

/* ---- Context-tagged encoders -------------------------------------------- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_uint(buf, cap, tag, true, v);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* context BOOLEAN: length 1, one contents octet X'00' or X'01' */
    size_t hl;
    int r = begin_prim(buf, cap, tag, true, 1u, &hl);
    if (r < 0) return -1;
    buf[hl] = v ? 1u : 0u;
    return r;
}

static int enc_pair_tag(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt)
{
    if (buf == NULL || tag == TAG_RESERVED) return -1;
    if (tag < TAG_EXTENDED) {
        if (cap < 1u) return -1;
        buf[0] = (uint8_t)((tag << 4) | CLASS_CONTEXT | lvt);
        return 1;
    }
    if (cap < 2u) return -1;
    buf[0] = (uint8_t)(0xF0u | CLASS_CONTEXT | lvt);
    buf[1] = tag;
    return 2;
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_pair_tag(buf, cap, tag, LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_pair_tag(buf, cap, tag, LVT_CLOSING);
}

/* ---- Decoders ------------------------------------------------------------ */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    size_t pos = 1;
    if (buf == NULL || out == NULL || len < 1u) return -1;

    uint8_t b0 = buf[0];
    uint8_t lvt = b0 & 0x07u;
    t.tag = (uint8_t)(b0 >> 4);
    t.context = (b0 & CLASS_CONTEXT) != 0u;
    t.opening = false;
    t.closing = false;
    t.lvt = 0;

    if (t.tag == TAG_EXTENDED) {
        if (pos >= len) return -1;
        uint8_t ext = buf[pos++];
        if (ext == TAG_RESERVED || ext < TAG_EXTENDED) return -1; /* reserved / non-minimal */
        t.tag = ext;
    }

    if (lvt == LVT_OPENING || lvt == LVT_CLOSING) {
        if (!t.context) return -1; /* opening/closing tags are context-class only */
        t.opening = (lvt == LVT_OPENING);
        t.closing = (lvt == LVT_CLOSING);
    } else if (lvt == LVT_EXTENDED) {
        if (pos >= len) return -1;
        uint8_t e = buf[pos++];
        if (e <= 253u) {
            if (e < 5u) return -1;
            t.lvt = e;
        } else if (e == 254u) {
            if (len - pos < 2u) return -1;
            t.lvt = get_be(buf + pos, 2u);
            pos += 2u;
            if (t.lvt < 254u) return -1;
        } else {
            if (len - pos < 4u) return -1;
            t.lvt = get_be(buf + pos, 4u);
            pos += 4u;
            if (t.lvt < 65536u) return -1;
        }
    } else {
        t.lvt = lvt;
    }

    *out = t;
    return (int)pos;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    bac_value_t v;
    if (out == NULL) return -1;
    int hr = bac_dec_tag(buf, len, &t);
    if (hr < 0 || t.context) return -1;
    size_t hl = (size_t)hr;

    v.tag = t.tag;
    if (t.tag == BAC_TAG_BOOLEAN) {
        if (t.lvt > 1u) return -1;
        v.v.boolean = (t.lvt == 1u);
        *out = v;
        return hr;
    }

    if ((uint64_t)t.lvt > (uint64_t)(len - hl)) return -1; /* truncated */
    size_t n = (size_t)t.lvt;
    if (n > (size_t)INT_MAX - hl) return -1;
    const uint8_t *p = buf + hl;

    switch (t.tag) {
    case BAC_TAG_NULL:
        if (n != 0u) return -1;
        break;
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        if (n < 1u || n > 4u) return -1;
        if (n > 1u && p[0] == 0x00u) return -1; /* not minimal */
        v.v.u = get_be(p, n);
        break;
    case BAC_TAG_SIGNED: {
        if (n < 1u || n > 4u) return -1;
        if (n > 1u && ((p[0] == 0x00u && !(p[1] & 0x80u)) || (p[0] == 0xFFu && (p[1] & 0x80u))))
            return -1; /* not minimal */
        uint32_t u = get_be(p, n);
        if (n < 4u && (p[0] & 0x80u)) u |= 0xFFFFFFFFu << (8u * n);
        v.v.i = (u <= (uint32_t)INT32_MAX) ? (int32_t)u : -(int32_t)(~u) - 1;
        break;
    }
    case BAC_TAG_REAL:
        if (n != 4u) return -1;
        v.v.r = bits_float(get_be(p, 4u));
        break;
    case BAC_TAG_OCTET_STRING:
        v.v.octets.data = p;
        v.v.octets.len = n;
        break;
    case BAC_TAG_CHARACTER_STRING: {
        if (n < 1u) return -1;
        uint8_t cs = p[0];
        size_t sl = n - 1u;
        if (cs == 0u && !utf8_valid(p + 1, sl)) return -1; /* UTF-8 */
        if (cs == 3u && (sl % 4u) != 0u) return -1;        /* UCS-4 */
        if (cs == 4u && (sl % 2u) != 0u) return -1;        /* UCS-2 */
        v.v.str.charset = cs;
        v.v.str.data = p + 1;
        v.v.str.len = sl;
        break;
    }
    case BAC_TAG_BIT_STRING: {
        if (n < 1u) return -1;
        uint8_t unused = p[0];
        if (unused > 7u || (n == 1u && unused != 0u)) return -1;
        if (n - 1u > SIZE_MAX / 8u) return -1;
        v.v.bits.data = p + 1;
        v.v.bits.nbits = (n - 1u) * 8u - unused;
        break;
    }
    case BAC_TAG_DATE:
        if (n != 4u) return -1;
        if (!date_fields_valid(p[1], p[2], p[3])) return -1;
        v.v.date.year = (p[0] == 255u) ? (uint16_t)BAC_YEAR_UNSPECIFIED : (uint16_t)(1900u + p[0]);
        v.v.date.month = p[1];
        v.v.date.day = p[2];
        v.v.date.wday = p[3];
        break;
    case BAC_TAG_TIME:
        if (n != 4u) return -1;
        if (!time_fields_valid(p[0], p[1], p[2], p[3])) return -1;
        v.v.time.hour = p[0];
        v.v.time.minute = p[1];
        v.v.time.second = p[2];
        v.v.time.hundredths = p[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        if (n != 4u) return -1;
        uint32_t raw = get_be(p, 4u);
        v.v.oid.type = (uint16_t)(raw >> 22);
        v.v.oid.instance = raw & MAX_OID_INSTANCE;
        break;
    }
    default:
        /* DOUBLE has no representation in bac_value_t; 13/14 and >= 15 are reserved */
        return -1;
    }

    *out = v;
    return (int)(hl + n);
}

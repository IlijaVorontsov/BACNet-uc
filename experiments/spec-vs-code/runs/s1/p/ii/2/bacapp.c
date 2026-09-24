/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Freestanding C11: no heap, no stdio, no libc calls at all.
 *
 * Design rules
 *  - Encoders always emit the canonical form: shortest tag-number form,
 *    shortest length form, fewest content octets for integers.
 *  - Encoders validate every argument and the capacity BEFORE touching buf,
 *    so a -1 return never leaves a partially written buffer.
 *  - Decoders never read outside buf[0..len) and write *out only on success.
 *  - Decoders reject structural errors (truncation, wrong fixed length,
 *    missing mandatory octets, values that do not fit the API types,
 *    reserved tag numbers) but accept non-canonical yet unambiguous forms
 *    (extended length/tag used where a short form would do, integers with
 *    redundant leading octets) and pass field values through unchanged
 *    (date/time fields, character-set contents, bit-string pad bits).
 */
#include "bacapp.h"

#include <limits.h>

#define TAG_NUMBER_MAX 254u  /* X'FF' as extended tag number is reserved */
#define TAG_EXTENDED 15u     /* tag-number field value meaning "next octet" */
#define CLASS_CONTEXT 0x08u
#define LVT_EXTENDED 5u
#define LVT_OPENING 6u
#define LVT_CLOSING 7u

/* ======================================================================== */
/* Common helpers                                                           */
/* ======================================================================== */

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    while (n > 0u) {
        n--;
        p[n] = (uint8_t)(v & 0xFFu);
        v >>= 8;
    }
}

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0u;
    for (size_t i = 0u; i < n; i++) {
        v = (v << 8) | p[i];
    }
    return v;
}

/* Length of a tag header for tag number `tag` and content length `len`. */
static size_t hdr_len(unsigned tag, size_t len)
{
    size_t n = (tag >= TAG_EXTENDED) ? 2u : 1u;
    if (len <= 4u) {
        return n;
    }
    if (len <= 253u) {
        return n + 1u;
    }
    if (len <= 65535u) {
        return n + 3u;
    }
    return n + 5u;
}

/* Write a primitive tag header. Caller has checked that it fits. */
static size_t put_hdr(uint8_t *buf, unsigned tag, bool ctx, size_t len)
{
    size_t pos = 1u;
    uint8_t b0 = ctx ? (uint8_t)CLASS_CONTEXT : 0u;

    if (tag >= TAG_EXTENDED) {
        b0 |= (uint8_t)(TAG_EXTENDED << 4);
        buf[pos++] = (uint8_t)tag;
    } else {
        b0 |= (uint8_t)(tag << 4);
    }

    if (len <= 4u) {
        b0 |= (uint8_t)len;
    } else {
        b0 |= (uint8_t)LVT_EXTENDED;
        if (len <= 253u) {
            buf[pos++] = (uint8_t)len;
        } else if (len <= 65535u) {
            buf[pos++] = 254u;
            put_be(&buf[pos], (uint32_t)len, 2u);
            pos += 2u;
        } else {
            buf[pos++] = 255u;
            put_be(&buf[pos], (uint32_t)len, 4u);
            pos += 4u;
        }
    }
    buf[0] = b0;
    return pos;
}

/* Check that a primitive value with `clen` content octets can be encoded
 * into buf[0..cap). On success returns the total size and stores the header
 * length in *hl; returns -1 otherwise. Nothing is written. */
static int plan(const uint8_t *buf, size_t cap, unsigned tag, size_t clen, size_t *hl)
{
    size_t h;

    if (buf == NULL || tag > TAG_NUMBER_MAX) {
        return -1;
    }
#if SIZE_MAX > 0xFFFFFFFFu
    if (clen > 0xFFFFFFFFu) { /* length field is at most 32 bits */
        return -1;
    }
#endif
    h = hdr_len(tag, clen);
    if (clen > (size_t)INT_MAX - h) { /* result must be representable */
        return -1;
    }
    if (h + clen > cap) {
        return -1;
    }
    *hl = h;
    return (int)(h + clen);
}

/* Minimal octet count for an unsigned value (at least one octet). */
static size_t uint_octets(uint32_t v)
{
    if (v <= 0xFFu) {
        return 1u;
    }
    if (v <= 0xFFFFu) {
        return 2u;
    }
    if (v <= 0xFFFFFFu) {
        return 3u;
    }
    return 4u;
}

/* Minimal octet count for a two's-complement signed value. */
static size_t sint_octets(int32_t v)
{
    if (v >= -128 && v <= 127) {
        return 1u;
    }
    if (v >= -32768 && v <= 32767) {
        return 2u;
    }
    if (v >= -8388608L && v <= 8388607L) {
        return 3u;
    }
    return 4u;
}

/* Encode a primitive whose content is a big-endian integer of n octets. */
static int enc_be_value(uint8_t *buf, size_t cap, unsigned tag, bool ctx, uint32_t raw, size_t n)
{
    size_t hl;
    int total = plan(buf, cap, tag, n, &hl);
    if (total < 0) {
        return -1;
    }
    (void)put_hdr(buf, tag, ctx, n);
    put_be(&buf[hl], raw, n);
    return total;
}

/* Encode a primitive whose content is an optional prefix octet (prefix >= 0)
 * followed by `len` octets from data. */
static int enc_bytes(uint8_t *buf, size_t cap, unsigned tag, int prefix, const uint8_t *data, size_t len)
{
    size_t hl;
    size_t pl = (prefix >= 0) ? 1u : 0u;
    int total;

    if (data == NULL && len > 0u) {
        return -1;
    }
    if (len > SIZE_MAX - pl) {
        return -1;
    }
    total = plan(buf, cap, tag, len + pl, &hl);
    if (total < 0) {
        return -1;
    }
    (void)put_hdr(buf, tag, false, len + pl);
    if (pl != 0u) {
        buf[hl] = (uint8_t)prefix;
    }
    for (size_t i = 0u; i < len; i++) {
        buf[hl + pl + i] = data[i];
    }
    return total;
}

/* ======================================================================== */
/* Application-tagged encoders                                              */
/* ======================================================================== */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t hl;
    int total = plan(buf, cap, BAC_TAG_NULL, 0u, &hl);
    if (total < 0) {
        return -1;
    }
    (void)put_hdr(buf, BAC_TAG_NULL, false, 0u);
    return total;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* 20.2.3: application-tagged boolean carries the value in the LVT field */
    if (buf == NULL || cap < 1u) {
        return -1;
    }
    buf[0] = (uint8_t)((BAC_TAG_BOOLEAN << 4) | (v ? 1u : 0u));
    return 1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_be_value(buf, cap, BAC_TAG_UNSIGNED, false, v, uint_octets(v));
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    /* conversion to uint32_t is modulo 2^32, i.e. two's complement */
    return enc_be_value(buf, cap, BAC_TAG_SIGNED, false, (uint32_t)v, sint_octets(v));
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    union {
        float f;
        uint32_t u;
    } pun;
    pun.f = v; /* bit-exact copy, preserves NaN payloads and -0.0 */
    return enc_be_value(buf, cap, BAC_TAG_REAL, false, pun.u, 4u);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    return enc_bytes(buf, cap, BAC_TAG_OCTET_STRING, -1, data, len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    /* character set 0 = ISO 10646 (UTF-8) */
    return enc_bytes(buf, cap, BAC_TAG_CHARACTER_STRING, 0, (const uint8_t *)utf8, len);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t nbytes = nbits / 8u + ((nbits % 8u) != 0u ? 1u : 0u);
    size_t unused = (8u - nbits % 8u) % 8u;
    size_t hl;
    int total;

    if (bits == NULL && nbits > 0u) {
        return -1;
    }
    if (nbytes > SIZE_MAX - 1u) {
        return -1;
    }
    total = plan(buf, cap, BAC_TAG_BIT_STRING, nbytes + 1u, &hl);
    if (total < 0) {
        return -1;
    }
    (void)put_hdr(buf, BAC_TAG_BIT_STRING, false, nbytes + 1u);
    buf[hl] = (uint8_t)unused;
    for (size_t i = 0u; i < nbytes; i++) {
        buf[hl + 1u + i] = 0u;
    }
    for (size_t i = 0u; i < nbits; i++) {
        if (bits[i] != 0u) { /* any non-zero value means 1 */
            buf[hl + 1u + i / 8u] |= (uint8_t)(0x80u >> (i % 8u));
        }
    }
    return total;
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_be_value(buf, cap, BAC_TAG_ENUMERATED, false, v, uint_octets(v));
}

static bool in_range_or_any(unsigned v, unsigned lo, unsigned hi)
{
    return v == 255u || (v >= lo && v <= hi);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t y;

    if (year == BAC_YEAR_UNSPECIFIED) {
        y = 255u;
    } else if (year >= 1900u && year <= 1900u + 254u) {
        y = (uint8_t)(year - 1900u);
    } else {
        return -1;
    }
    /* month 13/14 = odd/even months; day 32 = last, 33/34 = odd/even days */
    if (!in_range_or_any(month, 1u, 14u) || !in_range_or_any(day, 1u, 34u) ||
        !in_range_or_any(wday, 1u, 7u)) {
        return -1;
    }
    return enc_be_value(buf, cap, BAC_TAG_DATE, false,
                        ((uint32_t)y << 24) | ((uint32_t)month << 16) | ((uint32_t)day << 8) | wday, 4u);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    if (!in_range_or_any(hour, 0u, 23u) || !in_range_or_any(minute, 0u, 59u) ||
        !in_range_or_any(second, 0u, 59u) || !in_range_or_any(hundredths, 0u, 99u)) {
        return -1;
    }
    return enc_be_value(buf, cap, BAC_TAG_TIME, false,
                        ((uint32_t)hour << 24) | ((uint32_t)minute << 16) | ((uint32_t)second << 8) | hundredths,
                        4u);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    /* 10-bit object type, 22-bit instance number */
    if (type > 0x3FFu || instance > 0x3FFFFFu) {
        return -1;
    }
    return enc_be_value(buf, cap, BAC_TAG_OBJECT_ID, false, ((uint32_t)type << 22) | instance, 4u);
}

/* ======================================================================== */
/* Context-tagged encoders                                                  */
/* ======================================================================== */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_be_value(buf, cap, tag, true, v, uint_octets(v));
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* 20.2.3: context-tagged boolean has one content octet */
    return enc_be_value(buf, cap, tag, true, v ? 1u : 0u, 1u);
}

static int enc_open_close(uint8_t *buf, size_t cap, uint8_t tag, unsigned lvt)
{
    if (buf == NULL || tag > TAG_NUMBER_MAX) {
        return -1;
    }
    if (tag < TAG_EXTENDED) {
        if (cap < 1u) {
            return -1;
        }
        buf[0] = (uint8_t)((unsigned)tag << 4 | CLASS_CONTEXT | lvt);
        return 1;
    }
    if (cap < 2u) {
        return -1;
    }
    buf[0] = (uint8_t)(TAG_EXTENDED << 4 | CLASS_CONTEXT | lvt);
    buf[1] = tag;
    return 2;
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_CLOSING);
}

/* ======================================================================== */
/* Decoders                                                                 */
/* ======================================================================== */

/* Decode a tag header; also reports the raw 3-bit LVT field (needed for the
 * application-tagged boolean, whose value lives there). */
static int dec_hdr(const uint8_t *buf, size_t len, bac_tag_t *t, unsigned *raw_lvt)
{
    size_t pos = 1u;
    unsigned b0;
    unsigned tag;
    unsigned lvt;
    bac_tag_t r;

    if (buf == NULL || len < 1u) {
        return -1;
    }
    b0 = buf[0];
    tag = b0 >> 4;
    lvt = b0 & 0x07u;

    r.context = (b0 & CLASS_CONTEXT) != 0u;
    r.opening = false;
    r.closing = false;
    r.lvt = 0u;

    if (tag == TAG_EXTENDED) {
        if (len < 2u) {
            return -1;
        }
        tag = buf[1];
        if (tag > TAG_NUMBER_MAX) {
            return -1; /* X'FF' reserved */
        }
        pos = 2u;
    }
    r.tag = (uint8_t)tag;

    if (lvt == LVT_OPENING || lvt == LVT_CLOSING) {
        if (!r.context) {
            return -1; /* only context tags can be opening/closing */
        }
        r.opening = (lvt == LVT_OPENING);
        r.closing = (lvt == LVT_CLOSING);
    } else if (lvt == LVT_EXTENDED) {
        unsigned ext;
        if (pos >= len) {
            return -1;
        }
        ext = buf[pos++];
        if (ext <= 253u) {
            r.lvt = ext;
        } else {
            size_t n = (ext == 254u) ? 2u : 4u;
            if (len - pos < n) {
                return -1;
            }
            r.lvt = get_be(&buf[pos], n);
            pos += n;
        }
    } else {
        r.lvt = lvt;
    }

    if (pos > (size_t)INT_MAX) {
        return -1;
    }
    *t = r;
    if (raw_lvt != NULL) {
        *raw_lvt = lvt;
    }
    return (int)pos;
}

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    int hl;

    if (out == NULL) {
        return -1;
    }
    hl = dec_hdr(buf, len, &t, NULL);
    if (hl < 0) {
        return -1;
    }
    *out = t;
    return hl;
}

/* Unsigned integer from n >= 1 content octets; redundant leading zero
 * octets are accepted, values above 2^32-1 are rejected. */
static bool dec_uint(const uint8_t *c, size_t n, uint32_t *v)
{
    if (n == 0u) {
        return false;
    }
    while (n > 4u) {
        if (*c != 0u) {
            return false;
        }
        c++;
        n--;
    }
    *v = get_be(c, n);
    return true;
}

/* Two's-complement signed integer from n >= 1 content octets; redundant
 * sign-extension octets are accepted, values outside int32_t are rejected. */
static bool dec_sint(const uint8_t *c, size_t n, int32_t *v)
{
    uint32_t u;

    if (n == 0u) {
        return false;
    }
    while (n > 4u) {
        uint8_t fill = (c[1] & 0x80u) ? 0xFFu : 0x00u;
        if (c[0] != fill) {
            return false;
        }
        c++;
        n--;
    }
    u = (c[0] & 0x80u) ? 0xFFFFFFFFu : 0u; /* sign-extend */
    for (size_t i = 0u; i < n; i++) {
        u = (u << 8) | c[i];
    }
    /* portable uint32 -> int32 conversion (no implementation-defined cast) */
    if (u <= 0x7FFFFFFFu) {
        *v = (int32_t)u;
    } else {
        *v = -(int32_t)(~u) - 1;
    }
    return true;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    bac_value_t v;
    unsigned raw_lvt = 0u;
    const uint8_t *c;
    size_t n;
    size_t hl;
    int h;

    if (out == NULL) {
        return -1;
    }
    h = dec_hdr(buf, len, &t, &raw_lvt);
    if (h < 0 || t.context || t.opening || t.closing) {
        return -1;
    }
    hl = (size_t)h;
    v.tag = t.tag;

    if (t.tag == BAC_TAG_BOOLEAN) {
        /* value is the raw LVT field; there are no content octets */
        if (raw_lvt > 1u) {
            return -1;
        }
        v.v.boolean = (raw_lvt == 1u);
        *out = v;
        return h;
    }

    if (t.lvt > len - hl || t.lvt > (size_t)INT_MAX - hl) {
        return -1; /* content truncated, or size not representable */
    }
    c = &buf[hl];
    n = (size_t)t.lvt;

    switch (t.tag) {
    case BAC_TAG_NULL:
        if (n != 0u) {
            return -1;
        }
        break;
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        if (!dec_uint(c, n, &v.v.u)) {
            return -1;
        }
        break;
    case BAC_TAG_SIGNED:
        if (!dec_sint(c, n, &v.v.i)) {
            return -1;
        }
        break;
    case BAC_TAG_REAL: {
        union {
            uint32_t u;
            float f;
        } pun;
        if (n != 4u) {
            return -1;
        }
        pun.u = get_be(c, 4u);
        v.v.r = pun.f;
        break;
    }
    case BAC_TAG_OCTET_STRING:
        v.v.octets.data = c;
        v.v.octets.len = n;
        break;
    case BAC_TAG_CHARACTER_STRING:
        if (n < 1u) {
            return -1; /* character-set octet is mandatory */
        }
        v.v.str.charset = c[0];
        v.v.str.data = c + 1;
        v.v.str.len = n - 1u;
        break;
    case BAC_TAG_BIT_STRING:
        if (n < 1u || c[0] > 7u || (n == 1u && c[0] != 0u)) {
            return -1; /* missing/invalid unused-bits octet */
        }
        if (n - 1u > SIZE_MAX / 8u) {
            return -1; /* bit count not representable */
        }
        v.v.bits.data = c + 1;
        v.v.bits.nbits = (n - 1u) * 8u - c[0];
        break;
    case BAC_TAG_DATE:
        if (n != 4u) {
            return -1;
        }
        v.v.date.year = (c[0] == 255u) ? (uint16_t)BAC_YEAR_UNSPECIFIED : (uint16_t)(1900u + c[0]);
        v.v.date.month = c[1];
        v.v.date.day = c[2];
        v.v.date.wday = c[3];
        break;
    case BAC_TAG_TIME:
        if (n != 4u) {
            return -1;
        }
        v.v.time.hour = c[0];
        v.v.time.minute = c[1];
        v.v.time.second = c[2];
        v.v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        uint32_t raw;
        if (n != 4u) {
            return -1;
        }
        raw = get_be(c, 4u);
        v.v.oid.type = (uint16_t)(raw >> 22);
        v.v.oid.instance = raw & 0x3FFFFFu;
        break;
    }
    default:
        /* DOUBLE has no representation in bac_value_t; 13+ are reserved */
        return -1;
    }

    *out = v;
    return (int)(hl + n);
}

/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Bare-metal friendly: C11, no heap, no stdio, no libc calls at all (only
 * freestanding headers).  Byte order is handled with shifts, so the code does
 * not depend on host endianness.
 *
 * Behavioural contract (beyond what bacapp.h states):
 *
 * Encoders
 *   - Always emit the canonical form: shortest integer contents, shortest
 *     length form, extended tag number only for tags 15..254.
 *   - On any error they return -1 and leave buf completely untouched: the
 *     total size is computed and checked against cap before the first write.
 *   - Rejected input: buf == NULL, NULL data with a non-zero length, context
 *     tag 255 (reserved), results longer than INT_MAX, date/time fields outside
 *     the ranges of 20.2.12/20.2.13, year outside 1900..2154, object type
 *     > 1023, object instance > 4194303.
 *   - Character strings are emitted with character set 0 (ISO 10646 / UTF-8);
 *     the text bytes are copied verbatim (not validated).
 *   - Bit strings: any non-zero bits[i] counts as 1; unused trailing bits of
 *     the last octet are emitted as 0.
 *
 * Decoders
 *   - Never read outside buf[0..len) and never modify *out on failure.
 *   - Reject: truncated input, application-class opening/closing tags,
 *     extended tag number 255, context-class data in bac_dec_app, Boolean with
 *     an LVT other than B'000'/B'001', Null with content, Unsigned/Signed/
 *     Enumerated with 0 or more than 4 content octets, Real/Date/Time/ObjectId
 *     whose length is not 4, Character String without the charset octet, Bit
 *     String with more than 7 unused bits (or unused bits but no data octets),
 *     Date/Time fields outside their defined ranges, Double (the value union
 *     has no member for it) and reserved application tags 13..15.
 *   - Accept (unambiguous, merely non-canonical): integers with redundant
 *     leading octets, lengths/tag numbers in a longer-than-necessary form,
 *     non-zero padding bits in a bit string, any character-set code.
 */
#include "bacapp.h"

#include <limits.h>

_Static_assert(CHAR_BIT == 8, "octet-addressable target required");
_Static_assert(sizeof(float) == 4, "REAL requires a 32-bit IEEE-754 float");

#define CLASS_CONTEXT 0x08u
#define LVT_EXTENDED 5u
#define LVT_OPENING 6u
#define LVT_CLOSING 7u
#define TAG_EXTENDED 15u
#define TAG_RESERVED 255u

#define OID_MAX_TYPE 0x3FFu
#define OID_MAX_INSTANCE 0x3FFFFFu

#define UNSPEC 255u

/* ---- helpers ------------------------------------------------------------ */

/* Octets needed for a tag header with this tag number and content length. */
static size_t header_len(uint8_t tag, uint32_t len)
{
    size_t n = 1u;
    if (tag >= TAG_EXTENDED) {
        n += 1u;
    }
    if (len > 4u) {
        if (len <= 253u) {
            n += 1u;
        } else if (len <= 65535u) {
            n += 3u;
        } else {
            n += 5u;
        }
    }
    return n;
}

/* Write the first octet(s) (tag number, class, 3-bit LVT field). */
static size_t put_tag_octets(uint8_t *buf, uint8_t tag, bool context, uint8_t lvt3)
{
    uint8_t cls = context ? (uint8_t)CLASS_CONTEXT : 0u;
    if (tag >= TAG_EXTENDED) {
        buf[0] = (uint8_t)((TAG_EXTENDED << 4) | cls | lvt3);
        buf[1] = tag;
        return 2u;
    }
    buf[0] = (uint8_t)((unsigned)(tag << 4) | cls | lvt3);
    return 1u;
}

static void put_be(uint8_t *dst, uint32_t v, size_t n)
{
    while (n > 0u) {
        n--;
        *dst++ = (uint8_t)(v >> (8u * n));
    }
}

/* Write a primitive tag header for a value of len content octets. */
static size_t put_header(uint8_t *buf, uint8_t tag, bool context, uint32_t len)
{
    size_t i;
    if (len <= 4u) {
        return put_tag_octets(buf, tag, context, (uint8_t)len);
    }
    i = put_tag_octets(buf, tag, context, (uint8_t)LVT_EXTENDED);
    if (len <= 253u) {
        buf[i++] = (uint8_t)len;
    } else if (len <= 65535u) {
        buf[i++] = 254u;
        put_be(&buf[i], len, 2u);
        i += 2u;
    } else {
        buf[i++] = 255u;
        put_be(&buf[i], len, 4u);
        i += 4u;
    }
    return i;
}

/* Validate that a primitive value with len content octets fits, and if so
 * write its header.  Returns the header length, or 0 if the value cannot be
 * encoded (nothing is written in that case). */
static size_t begin_value(uint8_t *buf, size_t cap, uint8_t tag, bool context, size_t len)
{
    size_t hl;
    if (buf == NULL || tag == TAG_RESERVED || len > (size_t)UINT32_MAX) {
        return 0u;
    }
    hl = header_len(tag, (uint32_t)len);
    if (len > (size_t)INT_MAX - hl || hl + len > cap) {
        return 0u;
    }
    return put_header(buf, tag, context, (uint32_t)len);
}

/* Minimal number of octets for an unsigned value (1..4). */
static size_t unsigned_len(uint32_t v)
{
    if (v < 0x100u) {
        return 1u;
    }
    if (v < 0x10000u) {
        return 2u;
    }
    if (v < 0x1000000u) {
        return 3u;
    }
    return 4u;
}

/* Minimal number of two's-complement octets for a signed value (1..4). */
static size_t signed_len(int32_t v)
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

static int enc_unsigned_tagged(uint8_t *buf, size_t cap, uint8_t tag, bool context, uint32_t v)
{
    size_t n = unsigned_len(v);
    size_t hl = begin_value(buf, cap, tag, context, n);
    if (hl == 0u) {
        return -1;
    }
    put_be(&buf[hl], v, n);
    return (int)(hl + n);
}

static int enc_fixed4(uint8_t *buf, size_t cap, uint8_t tag, const uint8_t c[4])
{
    size_t k;
    size_t hl = begin_value(buf, cap, tag, false, 4u);
    if (hl == 0u) {
        return -1;
    }
    for (k = 0; k < 4u; k++) {
        buf[hl + k] = c[k];
    }
    return (int)(hl + 4u);
}

static uint32_t get_be(const uint8_t *src, size_t n)
{
    uint32_t v = 0;
    size_t k;
    for (k = 0; k < n; k++) {
        v = (v << 8) | src[k];
    }
    return v;
}

static bool date_fields_valid(uint8_t month, uint8_t day, uint8_t wday)
{
    /* 20.2.12: month 1..12, 13 = odd, 14 = even; day 1..31, 32 = last,
     * 33 = odd, 34 = even; day of week 1 (Monday)..7; 255 = unspecified. */
    return (month == UNSPEC || (month >= 1u && month <= 14u)) &&
           (day == UNSPEC || (day >= 1u && day <= 34u)) &&
           (wday == UNSPEC || (wday >= 1u && wday <= 7u));
}

static bool time_fields_valid(uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    /* 20.2.13: each field may be 255 = unspecified. */
    return (hour == UNSPEC || hour <= 23u) &&
           (minute == UNSPEC || minute <= 59u) &&
           (second == UNSPEC || second <= 59u) &&
           (hundredths == UNSPEC || hundredths <= 99u);
}

/* ---- application-tagged encoders ---------------------------------------- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    if (buf == NULL || cap < 1u) {
        return -1;
    }
    buf[0] = (uint8_t)(BAC_TAG_NULL << 4);
    return 1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* 20.2.3: the value lives in the LVT field, there are no content octets. */
    if (buf == NULL || cap < 1u) {
        return -1;
    }
    buf[0] = (uint8_t)((BAC_TAG_BOOLEAN << 4) | (v ? 1u : 0u));
    return 1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_unsigned_tagged(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_unsigned_tagged(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    size_t n = signed_len(v);
    size_t hl = begin_value(buf, cap, BAC_TAG_SIGNED, false, n);
    if (hl == 0u) {
        return -1;
    }
    put_be(&buf[hl], (uint32_t)v, n);
    return (int)(hl + n);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    union {
        float f;
        uint32_t u;
    } pun;
    uint8_t c[4];
    pun.f = v;
    put_be(c, pun.u, 4u);
    return enc_fixed4(buf, cap, BAC_TAG_REAL, c);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    size_t hl;
    size_t k;
    if (data == NULL && len != 0u) {
        return -1;
    }
    hl = begin_value(buf, cap, BAC_TAG_OCTET_STRING, false, len);
    if (hl == 0u) {
        return -1;
    }
    for (k = 0; k < len; k++) {
        buf[hl + k] = data[k];
    }
    return (int)(hl + len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    size_t hl;
    size_t k;
    if ((utf8 == NULL && len != 0u) || len == SIZE_MAX) {
        return -1;
    }
    hl = begin_value(buf, cap, BAC_TAG_CHARACTER_STRING, false, len + 1u);
    if (hl == 0u) {
        return -1;
    }
    buf[hl] = 0u; /* character set: ISO 10646 (UTF-8) */
    for (k = 0; k < len; k++) {
        buf[hl + 1u + k] = (uint8_t)utf8[k];
    }
    return (int)(hl + 1u + len);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t nbytes = nbits / 8u + ((nbits % 8u) != 0u ? 1u : 0u);
    size_t hl;
    size_t k;
    uint8_t *p;
    if ((bits == NULL && nbits != 0u) || nbytes == SIZE_MAX) {
        return -1;
    }
    hl = begin_value(buf, cap, BAC_TAG_BIT_STRING, false, nbytes + 1u);
    if (hl == 0u) {
        return -1;
    }
    p = &buf[hl];
    p[0] = (uint8_t)((8u - nbits % 8u) % 8u); /* unused bits in last octet */
    for (k = 0; k < nbytes; k++) {
        uint8_t acc = 0u;
        size_t b;
        for (b = 0; b < 8u && k * 8u + b < nbits; b++) {
            if (bits[k * 8u + b] != 0u) {
                acc |= (uint8_t)(0x80u >> b);
            }
        }
        p[1u + k] = acc;
    }
    return (int)(hl + 1u + nbytes);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t c[4];
    if (year == BAC_YEAR_UNSPECIFIED) {
        c[0] = UNSPEC;
    } else if (year >= 1900u && year <= 1900u + 254u) {
        c[0] = (uint8_t)(year - 1900u);
    } else {
        return -1;
    }
    if (!date_fields_valid(month, day, wday)) {
        return -1;
    }
    c[1] = month;
    c[2] = day;
    c[3] = wday;
    return enc_fixed4(buf, cap, BAC_TAG_DATE, c);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    uint8_t c[4];
    if (!time_fields_valid(hour, minute, second, hundredths)) {
        return -1;
    }
    c[0] = hour;
    c[1] = minute;
    c[2] = second;
    c[3] = hundredths;
    return enc_fixed4(buf, cap, BAC_TAG_TIME, c);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > OID_MAX_TYPE || instance > OID_MAX_INSTANCE) {
        return -1;
    }
    put_be(c, ((uint32_t)type << 22) | instance, 4u);
    return enc_fixed4(buf, cap, BAC_TAG_OBJECT_ID, c);
}

/* ---- context-tagged encoders -------------------------------------------- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_unsigned_tagged(buf, cap, tag, true, v);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* 20.2.3: context-tagged Boolean has one content octet (X'00' / X'01'). */
    size_t hl = begin_value(buf, cap, tag, true, 1u);
    if (hl == 0u) {
        return -1;
    }
    buf[hl] = v ? 1u : 0u;
    return (int)(hl + 1u);
}

static int enc_construct_tag(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt3)
{
    size_t need = (tag >= TAG_EXTENDED) ? 2u : 1u;
    if (buf == NULL || tag == TAG_RESERVED || cap < need) {
        return -1;
    }
    return (int)put_tag_octets(buf, tag, true, lvt3);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_construct_tag(buf, cap, tag, (uint8_t)LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_construct_tag(buf, cap, tag, (uint8_t)LVT_CLOSING);
}

/* ---- decoders ----------------------------------------------------------- */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    size_t i = 0;
    uint8_t lvt3;

    if (buf == NULL || out == NULL || len < 1u) {
        return -1;
    }
    t.tag = (uint8_t)(buf[0] >> 4);
    t.context = (buf[0] & CLASS_CONTEXT) != 0u;
    t.opening = false;
    t.closing = false;
    t.lvt = 0;
    lvt3 = (uint8_t)(buf[0] & 0x07u);
    i = 1u;

    if (t.tag == TAG_EXTENDED) {
        if (len < 2u || buf[1] == TAG_RESERVED) {
            return -1;
        }
        t.tag = buf[1];
        i = 2u;
    }

    if (lvt3 == LVT_OPENING || lvt3 == LVT_CLOSING) {
        /* 20.2.1.3.2: opening/closing tags are always context specific. */
        if (!t.context) {
            return -1;
        }
        t.opening = (lvt3 == LVT_OPENING);
        t.closing = (lvt3 == LVT_CLOSING);
    } else if (lvt3 == LVT_EXTENDED) {
        uint8_t e;
        if (len - i < 1u) {
            return -1;
        }
        e = buf[i++];
        if (e <= 253u) {
            t.lvt = e;
        } else {
            size_t n = (e == 254u) ? 2u : 4u;
            if (len - i < n) {
                return -1;
            }
            t.lvt = get_be(&buf[i], n);
            i += n;
        }
    } else {
        t.lvt = lvt3;
    }

    *out = t;
    return (int)i;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    bac_value_t v;
    const uint8_t *c;
    size_t hl;
    size_t n;
    int r;

    if (out == NULL) {
        return -1;
    }
    r = bac_dec_tag(buf, len, &t);
    if (r < 0 || t.context) {
        return -1;
    }
    hl = (size_t)r;
    v.tag = t.tag;

    if (t.tag == BAC_TAG_BOOLEAN) {
        /* The value is the raw LVT field itself; an extended length is invalid. */
        uint8_t lvt3 = (uint8_t)(buf[0] & 0x07u);
        if (lvt3 > 1u) {
            return -1;
        }
        v.v.boolean = (lvt3 == 1u);
        *out = v;
        return r;
    }

    if ((uint64_t)t.lvt > (uint64_t)(len - hl) || (uint64_t)t.lvt > (uint64_t)INT_MAX - hl) {
        return -1;
    }
    n = (size_t)t.lvt;
    c = &buf[hl];

    switch (t.tag) {
    case BAC_TAG_NULL:
        if (n != 0u) {
            return -1;
        }
        break;
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        if (n < 1u || n > 4u) {
            return -1;
        }
        v.v.u = get_be(c, n);
        break;
    case BAC_TAG_SIGNED: {
        uint32_t u;
        if (n < 1u || n > 4u) {
            return -1;
        }
        u = get_be(c, n);
        if (n < 4u && (c[0] & 0x80u) != 0u) {
            u |= ~(uint32_t)0 << (8u * n); /* sign-extend */
        }
        /* two's-complement reinterpretation without implementation-defined conversion */
        v.v.i = (u <= (uint32_t)INT32_MAX) ? (int32_t)u : (int32_t)(-(int32_t)(~u) - 1);
        break;
    }
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
            return -1;
        }
        v.v.str.charset = c[0];
        v.v.str.data = c + 1;
        v.v.str.len = n - 1u;
        break;
    case BAC_TAG_BIT_STRING: {
        uint8_t unused;
        if (n < 1u) {
            return -1;
        }
        unused = c[0];
        if (unused > 7u || (n == 1u && unused != 0u) || (n - 1u) > SIZE_MAX / 8u) {
            return -1;
        }
        v.v.bits.data = c + 1;
        v.v.bits.nbits = (n - 1u) * 8u - unused;
        break;
    }
    case BAC_TAG_DATE:
        if (n != 4u || !date_fields_valid(c[1], c[2], c[3])) {
            return -1;
        }
        v.v.date.year = (c[0] == UNSPEC) ? (uint16_t)BAC_YEAR_UNSPECIFIED : (uint16_t)(1900u + c[0]);
        v.v.date.month = c[1];
        v.v.date.day = c[2];
        v.v.date.wday = c[3];
        break;
    case BAC_TAG_TIME:
        if (n != 4u || !time_fields_valid(c[0], c[1], c[2], c[3])) {
            return -1;
        }
        v.v.time.hour = c[0];
        v.v.time.minute = c[1];
        v.v.time.second = c[2];
        v.v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        uint32_t u;
        if (n != 4u) {
            return -1;
        }
        u = get_be(c, 4u);
        v.v.oid.type = (uint16_t)(u >> 22);
        v.v.oid.instance = u & OID_MAX_INSTANCE;
        break;
    }
    case BAC_TAG_DOUBLE: /* no member in bac_value_t to hold it */
    default:             /* 13..15 reserved; >15 not application tags */
        return -1;
    }

    *out = v;
    return (int)(hl + n);
}

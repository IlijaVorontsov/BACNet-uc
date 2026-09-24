/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Implements the API in bacapp.h according to SPEC.md v1.0.
 * Bare-metal friendly: C11, no heap, no stdio; only <string.h> (memmove/memcpy).
 *
 * Encoder contract (G1): every encoder first validates its arguments and computes
 * the complete encoded size; only if everything is valid and fits into cap bytes is
 * anything written.  On error -1 is returned and buf is left untouched.
 *
 * Decoders never write to *out on error either.
 */
#include "bacapp.h"

#include <limits.h>
#include <string.h>

_Static_assert(sizeof(float) == 4, "BACnet REAL requires a 32-bit IEEE-754 float");
_Static_assert(CHAR_BIT == 8, "octet-addressable target required");

/* ---- Tag octet layout (G2) ---------------------------------------------- */
#define TAG_CLASS_CONTEXT 0x08u
#define TAG_LVT_MASK      0x07u
#define TAG_EXT_NUMBER    15u   /* bits 7..4 = 1111: tag number in next octet (G3) */
#define TAG_RESERVED      255u  /* reserved extended tag number (G3) */
#define LVT_EXT_LENGTH    5u    /* extended length follows (G4) */
#define LVT_OPENING       6u    /* opening tag (G5) */
#define LVT_CLOSING       7u    /* closing tag (G5) */
#define LEN_EXT16         254u  /* extended length: 2-octet length follows */
#define LEN_EXT32         255u  /* extended length: 4-octet length follows */

/* ---- Value limits --------------------------------------------------------- */
#define DATE_YEAR_BASE    1900u
#define DATE_YEAR_MAX     2154u     /* 1900 + 254; octet 255 means "unspecified" */
#define UNSPEC            255u      /* "unspecified" for date/time fields */
#define OID_TYPE_MAX      1023u
#define OID_INSTANCE_MAX  4194303uL
#define OID_TYPE_SHIFT    22u

/* ------------------------------------------------------------------------- */
/* Internal helpers                                                            */
/* ------------------------------------------------------------------------- */

/* Size in octets of a tag header for tag number `tag` and content length `clen`
 * (G3, G4, shortest form).  Returns 0 if the header cannot be encoded
 * (reserved tag 255, or a length that does not fit in 32 bits). */
static size_t header_size(uint8_t tag, size_t clen)
{
    size_t n;

    if (tag == TAG_RESERVED) {
        return 0u;
    }
    n = (tag >= TAG_EXT_NUMBER) ? 2u : 1u;
    if (clen <= 4u) {
        return n;
    }
    if (clen <= 253u) {
        return n + 1u;
    }
#if SIZE_MAX > 65535u
    if (clen <= 65535u) {
        return n + 3u;
    }
#endif
#if SIZE_MAX > 0xFFFFFFFFu
    if (clen > 0xFFFFFFFFu) {
        return 0u;
    }
#endif
#if SIZE_MAX > 65535u
    return n + 5u;
#else
    return n + 3u;
#endif
}

/* Validate that a value with tag number `tag` and content length `clen` can be
 * encoded into buf[0..cap).  On success stores the header size in *hl and returns
 * the total size; returns -1 otherwise.  Nothing is written. */
static int plan(const uint8_t *buf, size_t cap, uint8_t tag, size_t clen, size_t *hl)
{
    size_t h = header_size(tag, clen);

    if (buf == NULL || h == 0u) {
        return -1;
    }
    if (clen > cap || h > cap - clen) {
        return -1; /* does not fit */
    }
    if (h + clen > INT_MAX) {
        return -1; /* length not representable in the return type */
    }
    *hl = h;
    return (int)(h + clen);
}

static void put_be32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0u;
    size_t i;

    for (i = 0u; i < n; i++) {
        v = (v << 8) | p[i];
    }
    return v;
}

/* Write a tag header (space already checked by plan()).  Returns its size. */
static size_t put_header(uint8_t *buf, uint8_t tag, bool context, size_t clen)
{
    size_t i = 0u;
    uint8_t cls = context ? (uint8_t)TAG_CLASS_CONTEXT : 0u;
    uint8_t lvt = (clen <= 4u) ? (uint8_t)clen : (uint8_t)LVT_EXT_LENGTH;

    if (tag >= TAG_EXT_NUMBER) {
        buf[i++] = (uint8_t)((TAG_EXT_NUMBER << 4) | cls | lvt);
        buf[i++] = tag;
    } else {
        buf[i++] = (uint8_t)(((unsigned)tag << 4) | cls | lvt);
    }
    if (clen > 4u) {
        uint32_t l = (uint32_t)clen; /* header_size() guaranteed clen <= 0xFFFFFFFF */
        if (l <= 253u) {
            buf[i++] = (uint8_t)l;
        } else if (l <= 65535u) {
            buf[i++] = (uint8_t)LEN_EXT16;
            buf[i++] = (uint8_t)(l >> 8);
            buf[i++] = (uint8_t)l;
        } else {
            buf[i++] = (uint8_t)LEN_EXT32;
            put_be32(&buf[i], l);
            i += 4u;
        }
    }
    return i;
}

/* Minimum number of octets (1..4) for an unsigned value (E3). */
static size_t unsigned_octets(uint32_t v)
{
    if (v <= 0xFFuL) {
        return 1u;
    }
    if (v <= 0xFFFFuL) {
        return 2u;
    }
    if (v <= 0xFFFFFFuL) {
        return 3u;
    }
    return 4u;
}

/* Minimum number of octets (1..4) for a two's-complement signed value (E4). */
static size_t signed_octets(int32_t v)
{
    if (v >= -128L && v <= 127L) {
        return 1u;
    }
    if (v >= -32768L && v <= 32767L) {
        return 2u;
    }
    if (v >= -8388608L && v <= 8388607L) {
        return 3u;
    }
    return 4u;
}

/* Big-endian: the low n octets of v. */
static void put_be_n(uint8_t *p, uint32_t v, size_t n)
{
    size_t i;

    for (i = 0u; i < n; i++) {
        p[n - 1u - i] = (uint8_t)(v >> (8u * i));
    }
}

/* Encode a tag with an unsigned (E3) content. */
static int enc_uint_tagged(uint8_t *buf, size_t cap, uint8_t tag, bool context, uint32_t v)
{
    size_t n = unsigned_octets(v);
    size_t hl;
    int total = plan(buf, cap, tag, n, &hl);

    if (total < 0) {
        return -1;
    }
    (void)put_header(buf, tag, context, n);
    put_be_n(&buf[hl], v, n);
    return total;
}

/* Encode an application tag with 4 content octets. */
static int enc_app_4(uint8_t *buf, size_t cap, uint8_t tag, const uint8_t content[4])
{
    size_t hl;
    int total = plan(buf, cap, tag, 4u, &hl);

    if (total < 0) {
        return -1;
    }
    (void)put_header(buf, tag, false, 4u);
    buf[hl + 0u] = content[0];
    buf[hl + 1u] = content[1];
    buf[hl + 2u] = content[2];
    buf[hl + 3u] = content[3];
    return total;
}

/* Opening / closing tag (G5, E14). */
static int enc_open_close(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt)
{
    size_t n = (tag >= TAG_EXT_NUMBER) ? 2u : 1u;

    if (buf == NULL || tag == TAG_RESERVED || cap < n) {
        return -1;
    }
    if (tag >= TAG_EXT_NUMBER) {
        buf[0] = (uint8_t)((TAG_EXT_NUMBER << 4) | TAG_CLASS_CONTEXT | lvt);
        buf[1] = tag;
    } else {
        buf[0] = (uint8_t)(((unsigned)tag << 4) | TAG_CLASS_CONTEXT | lvt);
    }
    return (int)n;
}

static bool in_range_or_unspec(uint8_t v, uint8_t lo, uint8_t hi)
{
    return (v >= lo && v <= hi) || v == UNSPEC;
}

/* ------------------------------------------------------------------------- */
/* Application encoders                                                        */
/* ------------------------------------------------------------------------- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    if (buf == NULL || cap < 1u) {
        return -1;
    }
    buf[0] = (uint8_t)(BAC_TAG_NULL << 4); /* E1: 00 */
    return 1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    if (buf == NULL || cap < 1u) {
        return -1;
    }
    buf[0] = (uint8_t)((BAC_TAG_BOOLEAN << 4) | (v ? 1u : 0u)); /* E2: value in LVT */
    return 1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint_tagged(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint_tagged(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    size_t n = signed_octets(v);
    size_t hl;
    int total = plan(buf, cap, BAC_TAG_SIGNED, n, &hl);

    if (total < 0) {
        return -1;
    }
    (void)put_header(buf, BAC_TAG_SIGNED, false, n);
    put_be_n(&buf[hl], (uint32_t)v, n); /* conversion to unsigned is modulo 2^32 */
    return total;
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t c[4];

    memcpy(&bits, &v, sizeof bits); /* bit-exact, no float arithmetic (E5) */
    /* E5a: refuse every NaN (exponent all ones, non-zero mantissa). */
    if ((bits & 0x7F800000uL) == 0x7F800000uL && (bits & 0x007FFFFFuL) != 0u) {
        return -1;
    }
    put_be32(c, bits);
    return enc_app_4(buf, cap, BAC_TAG_REAL, c);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    size_t hl;
    int total;

    if (data == NULL && len != 0u) {
        return -1;
    }
    total = plan(buf, cap, BAC_TAG_OCTET_STRING, len, &hl);
    if (total < 0) {
        return -1;
    }
    /* Content first (memmove: tolerates data overlapping buf), then header. */
    if (len != 0u) {
        memmove(&buf[hl], data, len);
    }
    (void)put_header(buf, BAC_TAG_OCTET_STRING, false, len);
    return total;
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    size_t hl;
    size_t clen;
    int total;

    if ((utf8 == NULL && len != 0u) || len == SIZE_MAX) {
        return -1;
    }
    clen = len + 1u; /* charset octet + text (E7) */
    total = plan(buf, cap, BAC_TAG_CHARACTER_STRING, clen, &hl);
    if (total < 0) {
        return -1;
    }
    /* E7a: bytes are sent as given, no validation. */
    if (len != 0u) {
        memmove(&buf[hl + 1u], utf8, len);
    }
    buf[hl] = 0u; /* character set 0 = UTF-8 */
    (void)put_header(buf, BAC_TAG_CHARACTER_STRING, false, clen);
    return total;
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t nbytes;
    size_t hl;
    size_t i;
    int total;

    if (bits == NULL && nbits != 0u) {
        return -1;
    }
    nbytes = nbits / 8u + ((nbits % 8u) != 0u ? 1u : 0u);
    total = plan(buf, cap, BAC_TAG_BIT_STRING, nbytes + 1u, &hl);
    if (total < 0) {
        return -1;
    }
    for (i = 0u; i < nbits; i++) { /* E8a: bit array, not bit masks */
        if (bits[i] > 1u) {
            return -1;
        }
    }
    (void)put_header(buf, BAC_TAG_BIT_STRING, false, nbytes + 1u);
    buf[hl] = (uint8_t)((8u - nbits % 8u) % 8u); /* unused bits in last octet */
    for (i = 0u; i < nbytes; i++) {
        uint8_t octet = 0u;
        size_t k;
        for (k = 0u; k < 8u; k++) {
            size_t idx = i * 8u + k;
            if (idx < nbits && bits[idx] != 0u) {
                octet = (uint8_t)(octet | (0x80u >> k));
            }
        }
        buf[hl + 1u + i] = octet;
    }
    return total;
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t c[4];

    /* E9a */
    if (year == BAC_YEAR_UNSPECIFIED) {
        c[0] = (uint8_t)UNSPEC;
    } else if (year >= DATE_YEAR_BASE && year <= DATE_YEAR_MAX) {
        c[0] = (uint8_t)(year - DATE_YEAR_BASE);
    } else {
        return -1;
    }
    if (!in_range_or_unspec(month, 1u, 14u) || !in_range_or_unspec(day, 1u, 34u) ||
        !in_range_or_unspec(wday, 1u, 7u)) {
        return -1;
    }
    c[1] = month;
    c[2] = day;
    c[3] = wday;
    return enc_app_4(buf, cap, BAC_TAG_DATE, c);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    uint8_t c[4];

    /* E10a */
    if (!in_range_or_unspec(hour, 0u, 23u) || !in_range_or_unspec(minute, 0u, 59u) ||
        !in_range_or_unspec(second, 0u, 59u) || !in_range_or_unspec(hundredths, 0u, 99u)) {
        return -1;
    }
    c[0] = hour;
    c[1] = minute;
    c[2] = second;
    c[3] = hundredths;
    return enc_app_4(buf, cap, BAC_TAG_TIME, c);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];

    /* E11a: never truncate */
    if (type > OID_TYPE_MAX || instance > OID_INSTANCE_MAX) {
        return -1;
    }
    put_be32(c, ((uint32_t)type << OID_TYPE_SHIFT) | instance);
    return enc_app_4(buf, cap, BAC_TAG_OBJECT_ID, c);
}

/* ------------------------------------------------------------------------- */
/* Context encoders                                                            */
/* ------------------------------------------------------------------------- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_uint_tagged(buf, cap, tag, true, v); /* E12; tag 255 rejected by plan() */
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    size_t hl;
    int total = plan(buf, cap, tag, 1u, &hl);

    if (total < 0) {
        return -1;
    }
    (void)put_header(buf, tag, true, 1u);
    buf[hl] = v ? 1u : 0u; /* E13: length 1, content 00/01 */
    return total;
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, (uint8_t)LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, (uint8_t)LVT_CLOSING);
}

/* ------------------------------------------------------------------------- */
/* Decoders                                                                    */
/* ------------------------------------------------------------------------- */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    size_t pos = 1u;
    uint8_t lvt;

    if (buf == NULL || out == NULL || len == 0u) {
        return -1;
    }
    t.tag = (uint8_t)(buf[0] >> 4);
    t.context = (buf[0] & TAG_CLASS_CONTEXT) != 0u;
    t.opening = false;
    t.closing = false;
    lvt = (uint8_t)(buf[0] & TAG_LVT_MASK);

    if (t.tag == TAG_EXT_NUMBER) {
        if (len < 2u || buf[1] == TAG_RESERVED) {
            return -1; /* truncated, or reserved tag number 255 */
        }
        t.tag = buf[1]; /* D1b: values below 15 accepted */
        pos = 2u;
    }

    if (lvt == LVT_OPENING || lvt == LVT_CLOSING) {
        if (!t.context) {
            return -1; /* D1a: application class with LVT 6/7 */
        }
        t.opening = (lvt == LVT_OPENING);
        t.closing = (lvt == LVT_CLOSING);
        t.lvt = lvt; /* not meaningful */
    } else if (lvt == LVT_EXT_LENGTH) {
        /* D1: always an extended length, whatever tag number and class. */
        uint8_t l;
        if (pos >= len) {
            return -1;
        }
        l = buf[pos++];
        if (l == LEN_EXT16) {
            if (len - pos < 2u) {
                return -1;
            }
            t.lvt = get_be(&buf[pos], 2u);
            pos += 2u;
        } else if (l == LEN_EXT32) {
            if (len - pos < 4u) {
                return -1;
            }
            t.lvt = get_be(&buf[pos], 4u);
            pos += 4u;
        } else {
            t.lvt = l; /* D1b: non-minimal forms accepted */
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
    const uint8_t *c;
    size_t clen;
    int hl;
    uint8_t raw_lvt;

    if (out == NULL) {
        return -1;
    }
    hl = bac_dec_tag(buf, len, &t);
    if (hl < 0) {
        return -1;
    }
    /* D2: context class (incl. opening/closing), unsupported / reserved tags. */
    if (t.context || t.opening || t.closing) {
        return -1;
    }
    if (t.tag == BAC_TAG_DOUBLE || t.tag > BAC_TAG_OBJECT_ID) {
        return -1;
    }

    memset(&v, 0, sizeof v);
    v.tag = t.tag;
    raw_lvt = (uint8_t)(buf[0] & TAG_LVT_MASK);

    /* D3: Null and Boolean use the raw LVT bits; they have no content. */
    if (t.tag == BAC_TAG_NULL) {
        if (raw_lvt != 0u) {
            return -1;
        }
        *out = v;
        return hl;
    }
    if (t.tag == BAC_TAG_BOOLEAN) {
        if (raw_lvt > 1u) {
            return -1;
        }
        v.v.boolean = (raw_lvt == 1u);
        *out = v;
        return hl;
    }

    /* D2: content must lie within buf[0..len); total must fit the return type. */
    if (t.lvt > len - (size_t)hl) {
        return -1;
    }
    if (t.lvt > (unsigned)INT_MAX - (unsigned)hl) {
        return -1;
    }
    clen = (size_t)t.lvt;
    c = &buf[hl];

    switch (t.tag) {
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        /* D4 */
        if (clen < 1u || clen > 4u) {
            return -1;
        }
        v.v.u = get_be(c, clen);
        break;
    case BAC_TAG_SIGNED: {
        /* D4: sign-extend */
        uint32_t u;
        if (clen < 1u || clen > 4u) {
            return -1;
        }
        u = get_be(c, clen);
        if (clen < 4u && (c[0] & 0x80u) != 0u) {
            u |= (uint32_t)(UINT32_MAX << (8u * clen));
        }
        if (u > (uint32_t)INT32_MAX) {
            v.v.i = (int32_t)(-(int32_t)(uint32_t)~u - 1); /* portable two's complement */
        } else {
            v.v.i = (int32_t)u;
        }
        break;
    }
    case BAC_TAG_REAL: {
        /* D5: exactly 4 octets, NaN decoded like any other value */
        uint32_t bits;
        if (clen != 4u) {
            return -1;
        }
        bits = get_be(c, 4u);
        memcpy(&v.v.r, &bits, sizeof v.v.r);
        break;
    }
    case BAC_TAG_OCTET_STRING:
        /* D6 */
        v.v.octets.data = c;
        v.v.octets.len = clen;
        break;
    case BAC_TAG_CHARACTER_STRING:
        /* D7 */
        if (clen < 1u) {
            return -1;
        }
        v.v.str.charset = c[0];
        v.v.str.data = c + 1;
        v.v.str.len = clen - 1u;
        break;
    case BAC_TAG_BIT_STRING: {
        /* D8 */
        uint8_t unused;
        if (clen < 1u) {
            return -1;
        }
        unused = c[0];
        if (unused > 7u || (clen == 1u && unused != 0u)) {
            return -1;
        }
        if (clen - 1u > SIZE_MAX / 8u) {
            return -1;
        }
        v.v.bits.data = c + 1;
        v.v.bits.nbits = (clen - 1u) * 8u - unused;
        break;
    }
    case BAC_TAG_DATE:
        /* D9: no validation of field values */
        if (clen != 4u) {
            return -1;
        }
        v.v.date.year = (c[0] == UNSPEC) ? (uint16_t)BAC_YEAR_UNSPECIFIED
                                         : (uint16_t)(DATE_YEAR_BASE + c[0]);
        v.v.date.month = c[1];
        v.v.date.day = c[2];
        v.v.date.wday = c[3];
        break;
    case BAC_TAG_TIME:
        if (clen != 4u) {
            return -1;
        }
        v.v.time.hour = c[0];
        v.v.time.minute = c[1];
        v.v.time.second = c[2];
        v.v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        uint32_t id;
        if (clen != 4u) {
            return -1;
        }
        id = get_be(c, 4u);
        v.v.oid.type = (uint16_t)(id >> OID_TYPE_SHIFT);
        v.v.oid.instance = id & OID_INSTANCE_MAX;
        break;
    }
    default:
        return -1; /* 13, 14 and >= 15 (already filtered above) */
    }

    *out = v;
    return hl + (int)clen;
}

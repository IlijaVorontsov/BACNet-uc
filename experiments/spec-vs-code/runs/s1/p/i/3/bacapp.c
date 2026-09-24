/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Bare-metal friendly: C11, no heap, no stdio, no floating-point arithmetic
 * (REAL values are moved bit-for-bit), safe for 16/32/64-bit int and size_t.
 *
 * Policies (applied uniformly):
 *  - Encoders validate every argument and the capacity BEFORE touching buf.
 *    On error they return -1 and leave buf completely unmodified; they never
 *    write at or beyond buf[cap].  Output depends only on the arguments.
 *  - Encoders always produce the canonical form required by 20.2: shortest
 *    tag-number form, shortest length form, minimum number of integer octets,
 *    zeroed unused bits in bit strings.
 *  - Decoders accept only encodings the standard permits:
 *      * extended tag numbers must be 15..254 (20.2.1.2: the extended form is
 *        not allowed for 0..14; 255 is reserved);
 *      * extended lengths must use the form mandated for their range
 *        (20.2.1.3.1: 5..253, 254..65535, 65536..2^32-1);
 *      * unsigned/enumerated/signed contents must be 1..4 octets with no
 *        redundant leading octets (the value must fit the 32-bit API type);
 *      * fixed-size types (REAL, DATE, TIME, OBJECT_ID) must have length 4,
 *        NULL length 0, application BOOLEAN value 0 or 1;
 *      * bit strings: 0..7 unused bits, and 0 when the string is empty;
 *      * character strings need the character-set octet; charset 0 content
 *        must be well-formed UTF-8 (RFC 3629);
 *      * DATE/TIME fields must hold values defined for BACnetDate/BACnetTime.
 *    DOUBLE has no representation in bac_value_t and is rejected, as are the
 *    reserved application tags 13..15+ and context-class tags.
 *    Nonzero unused bits at the end of a bit string are tolerated (they carry
 *    no information).  *out is only written on success.
 */
#include "bacapp.h"

#include <limits.h>
#include <string.h>

_Static_assert(sizeof(float) == 4, "REAL requires a 32-bit IEEE-754 float");
_Static_assert(CHAR_BIT == 8, "octet-addressable target required");

#define CLASS_CONTEXT 0x08u
#define LVT_EXTENDED 5u
#define LVT_OPENING 6u
#define LVT_CLOSING 7u
#define TAG_EXTENDED 15u   /* tag number field value announcing an extended tag */
#define TAG_MAX 254u       /* 255 is reserved by ASHRAE (20.2.1.2) */
#define EXT_LEN_16 254u    /* extended length octet: 2-octet length follows */
#define EXT_LEN_32 255u    /* extended length octet: 4-octet length follows */

#define DATE_YEAR_MIN 1900u
#define DATE_YEAR_MAX (1900u + 254u) /* 255 in the year octet means "unspecified" */
#define UNSPEC 255u

/* ------------------------------------------------------------------------ */
/* Helpers                                                                   */
/* ------------------------------------------------------------------------ */

static size_t uint_octets(uint32_t v)
{
    if (v <= 0xFFu) return 1;
    if (v <= 0xFFFFu) return 2;
    if (v <= 0xFFFFFFu) return 3;
    return 4;
}

static size_t sint_octets(int32_t v)
{
    if (v >= -128 && v <= 127) return 1;
    if (v >= -32768 && v <= 32767) return 2;
    if (v >= -8388608L && v <= 8388607L) return 3;
    return 4;
}

/* Write the low n octets of v, most significant first. */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    while (n > 0) {
        n--;
        *p++ = (uint8_t)(v >> (8u * n));
    }
}

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    while (n-- > 0) v = (v << 8) | *p++;
    return v;
}

/* Header size for a primitive (or context) tag with content length len. */
static size_t prim_hdr_size(unsigned tag, uint32_t len)
{
    size_t n = (tag >= TAG_EXTENDED) ? 2u : 1u;
    if (len >= 5u) {
        if (len <= 253u) n += 1u;
        else if (len <= 0xFFFFu) n += 3u;
        else n += 5u;
    }
    return n;
}

/* Write a tag header. lvt_field is the 3-bit LVT value placed in the initial
 * octet; ext_len (only used when lvt_field == 5) is the extended length. */
static size_t put_hdr(uint8_t *p, unsigned tag, bool ctx, unsigned lvt_field, uint32_t ext_len)
{
    size_t n = 0;
    uint8_t first = (uint8_t)(((tag >= TAG_EXTENDED ? TAG_EXTENDED : tag) << 4) |
                              (ctx ? CLASS_CONTEXT : 0u) | lvt_field);
    p[n++] = first;
    if (tag >= TAG_EXTENDED) p[n++] = (uint8_t)tag;
    if (lvt_field == LVT_EXTENDED) {
        if (ext_len <= 253u) {
            p[n++] = (uint8_t)ext_len;
        } else if (ext_len <= 0xFFFFu) {
            p[n++] = (uint8_t)EXT_LEN_16;
            put_be(p + n, ext_len, 2);
            n += 2;
        } else {
            p[n++] = (uint8_t)EXT_LEN_32;
            put_be(p + n, ext_len, 4);
            n += 4;
        }
    }
    return n;
}

/* Check that a primitive value with content length len fits, then write its
 * header.  Returns the header length, or 0 (nothing written) if the value
 * cannot be encoded into cap bytes or its size cannot be returned as int. */
static size_t begin_prim(uint8_t *buf, size_t cap, unsigned tag, bool ctx, size_t len)
{
    size_t h;
    if (buf == NULL || tag > TAG_MAX) return 0;
#if SIZE_MAX > 0xFFFFFFFFu
    if (len > 0xFFFFFFFFu) return 0;
#endif
    h = prim_hdr_size(tag, (uint32_t)len);
    if (len > cap || h > cap - len) return 0;
    if (len > (size_t)INT_MAX || h > (size_t)INT_MAX - len) return 0;
    return put_hdr(buf, tag, ctx, len >= 5u ? LVT_EXTENDED : (unsigned)len, (uint32_t)len);
}

/* RFC 3629 well-formedness (rejects overlongs, surrogates, > U+10FFFF). */
static bool utf8_valid(const uint8_t *s, size_t n)
{
    size_t i = 0;
    while (i < n) {
        uint8_t c = s[i];
        uint8_t lo = 0x80u, hi = 0xBFu;
        size_t k, j;
        if (c < 0x80u) {
            i++;
            continue;
        } else if (c >= 0xC2u && c <= 0xDFu) {
            k = 1;
        } else if (c >= 0xE0u && c <= 0xEFu) {
            k = 2;
            if (c == 0xE0u) lo = 0xA0u;
            else if (c == 0xEDu) hi = 0x9Fu;
        } else if (c >= 0xF0u && c <= 0xF4u) {
            k = 3;
            if (c == 0xF0u) lo = 0x90u;
            else if (c == 0xF4u) hi = 0x8Fu;
        } else {
            return false;
        }
        if (n - i - 1u < k) return false;
        if (s[i + 1] < lo || s[i + 1] > hi) return false;
        for (j = 2; j <= k; j++)
            if ((s[i + j] & 0xC0u) != 0x80u) return false;
        i += k + 1u;
    }
    return true;
}

static bool date_fields_valid(uint8_t month, uint8_t day, uint8_t wday)
{
    /* month 1..12, 13 = odd, 14 = even; day 1..31, 32 = last, 33 = odd, 34 = even;
     * weekday 1 (Monday)..7; 255 = unspecified for all */
    if (!((month >= 1u && month <= 14u) || month == UNSPEC)) return false;
    if (!((day >= 1u && day <= 34u) || day == UNSPEC)) return false;
    if (!((wday >= 1u && wday <= 7u) || wday == UNSPEC)) return false;
    return true;
}

static bool time_fields_valid(uint8_t h, uint8_t m, uint8_t s, uint8_t hs)
{
    if (h > 23u && h != UNSPEC) return false;
    if (m > 59u && m != UNSPEC) return false;
    if (s > 59u && s != UNSPEC) return false;
    if (hs > 99u && hs != UNSPEC) return false;
    return true;
}

static int enc_uint(uint8_t *buf, size_t cap, unsigned tag, bool ctx, uint32_t v)
{
    size_t n = uint_octets(v);
    size_t h = begin_prim(buf, cap, tag, ctx, n);
    if (h == 0) return -1;
    put_be(buf + h, v, n);
    return (int)(h + n);
}

static int enc_fixed4(uint8_t *buf, size_t cap, unsigned tag, uint32_t v)
{
    size_t h = begin_prim(buf, cap, tag, false, 4);
    if (h == 0) return -1;
    put_be(buf + h, v, 4);
    return (int)(h + 4u);
}

/* ------------------------------------------------------------------------ */
/* Encoders                                                                  */
/* ------------------------------------------------------------------------ */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    if (buf == NULL || cap < 1u) return -1;
    buf[0] = (uint8_t)(BAC_TAG_NULL << 4);
    return 1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* 20.2.3: application-tagged BOOLEAN carries its value in the LVT field */
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
    size_t n = sint_octets(v);
    size_t h = begin_prim(buf, cap, BAC_TAG_SIGNED, false, n);
    if (h == 0) return -1;
    put_be(buf + h, (uint32_t)v, n); /* two's complement, low n octets */
    return (int)(h + n);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits); /* IEEE-754 single, bit-exact (NaN payloads, -0) */
    return enc_fixed4(buf, cap, BAC_TAG_REAL, bits);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    size_t h;
    if (data == NULL && len > 0u) return -1;
    h = begin_prim(buf, cap, BAC_TAG_OCTET_STRING, false, len);
    if (h == 0) return -1;
    if (len > 0u) memcpy(buf + h, data, len);
    return (int)(h + len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    const uint8_t *s = (const uint8_t *)utf8;
    size_t h;
    if (s == NULL && len > 0u) return -1;
    if (len == SIZE_MAX) return -1;
    if (len > 0u && !utf8_valid(s, len)) return -1;
    h = begin_prim(buf, cap, BAC_TAG_CHARACTER_STRING, false, len + 1u);
    if (h == 0) return -1;
    buf[h] = 0; /* character set 0: ISO 10646 (UTF-8) */
    if (len > 0u) memcpy(buf + h + 1u, s, len);
    return (int)(h + 1u + len);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t nbytes = nbits / 8u + ((nbits % 8u) ? 1u : 0u);
    size_t h, i;
    uint8_t *p;
    if (bits == NULL && nbits > 0u) return -1;
    if (nbytes == SIZE_MAX) return -1;
    h = begin_prim(buf, cap, BAC_TAG_BIT_STRING, false, nbytes + 1u);
    if (h == 0) return -1;
    p = buf + h;
    *p++ = (uint8_t)(nbytes * 8u - nbits); /* unused bits in the final octet */
    for (i = 0; i < nbytes; i++) {
        uint8_t o = 0;
        size_t b;
        for (b = 0; b < 8u && i * 8u + b < nbits; b++)
            if (bits[i * 8u + b]) o = (uint8_t)(o | (0x80u >> b));
        p[i] = o;
    }
    return (int)(h + 1u + nbytes);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t y;
    if (year == BAC_YEAR_UNSPECIFIED) y = UNSPEC;
    else if (year >= DATE_YEAR_MIN && year <= DATE_YEAR_MAX) y = (uint8_t)(year - DATE_YEAR_MIN);
    else return -1;
    if (!date_fields_valid(month, day, wday)) return -1;
    return enc_fixed4(buf, cap, BAC_TAG_DATE,
                      ((uint32_t)y << 24) | ((uint32_t)month << 16) | ((uint32_t)day << 8) | wday);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    if (!time_fields_valid(hour, minute, second, hundredths)) return -1;
    return enc_fixed4(buf, cap, BAC_TAG_TIME,
                      ((uint32_t)hour << 24) | ((uint32_t)minute << 16) | ((uint32_t)second << 8) | hundredths);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    /* 20.2.14: 10-bit object type, 22-bit instance number */
    if (type > 0x3FFu || instance > 0x3FFFFFu) return -1;
    return enc_fixed4(buf, cap, BAC_TAG_OBJECT_ID, ((uint32_t)type << 22) | instance);
}

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_uint(buf, cap, tag, true, v);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* context-tagged BOOLEAN: one content octet, 0 or 1 */
    size_t h = begin_prim(buf, cap, tag, true, 1);
    if (h == 0) return -1;
    buf[h] = v ? 1u : 0u;
    return (int)(h + 1u);
}

static int enc_open_close(uint8_t *buf, size_t cap, uint8_t tag, unsigned lvt)
{
    size_t n = (tag >= TAG_EXTENDED) ? 2u : 1u;
    if (buf == NULL || tag > TAG_MAX || cap < n) return -1;
    return (int)put_hdr(buf, tag, true, lvt, 0);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_CLOSING);
}

/* ------------------------------------------------------------------------ */
/* Decoders                                                                  */
/* ------------------------------------------------------------------------ */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    size_t pos = 1;
    unsigned lvt;

    if (buf == NULL || out == NULL || len < 1u) return -1;
    t.tag = (uint8_t)(buf[0] >> 4);
    t.context = (buf[0] & CLASS_CONTEXT) != 0u;
    t.opening = false;
    t.closing = false;
    t.lvt = 0;
    lvt = buf[0] & 0x07u;

    if (t.tag == TAG_EXTENDED) {
        if (len < 2u) return -1;
        if (buf[1] < TAG_EXTENDED || buf[1] > TAG_MAX) return -1; /* 20.2.1.2 */
        t.tag = buf[1];
        pos = 2;
    }

    if (lvt == LVT_OPENING || lvt == LVT_CLOSING) {
        if (!t.context) return -1; /* opening/closing tags are context-specific only */
        t.opening = (lvt == LVT_OPENING);
        t.closing = (lvt == LVT_CLOSING);
    } else if (lvt == LVT_EXTENDED) {
        uint8_t b;
        if (len - pos < 1u) return -1;
        b = buf[pos++];
        if (b < EXT_LEN_16) {
            if (b < 5u) return -1; /* 0..4 must use the LVT field */
            t.lvt = b;
        } else if (b == EXT_LEN_16) {
            if (len - pos < 2u) return -1;
            t.lvt = get_be(buf + pos, 2);
            pos += 2;
            if (t.lvt < 254u) return -1;
        } else {
            if (len - pos < 4u) return -1;
            t.lvt = get_be(buf + pos, 4);
            pos += 4;
            if (t.lvt < 0x10000ul) return -1;
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
    const uint8_t *p;
    size_t hl, L;
    int r;

    if (out == NULL) return -1;
    r = bac_dec_tag(buf, len, &t);
    if (r < 0 || t.context) return -1;
    hl = (size_t)r;

    memset(&v, 0, sizeof v);
    v.tag = t.tag;

    /* BOOLEAN: value in LVT, no content octets */
    if (t.tag == BAC_TAG_BOOLEAN) {
        if (hl != 1u || t.lvt > 1u) return -1;
        v.v.boolean = (t.lvt == 1u);
        *out = v;
        return 1;
    }

    if (t.lvt > len - hl) return -1; /* content runs past the buffer */
    if (t.lvt > (uint32_t)INT_MAX - hl) return -1;
    L = (size_t)t.lvt;
    p = buf + hl;

    switch (t.tag) {
    case BAC_TAG_NULL:
        if (L != 0u) return -1;
        break;
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        if (L < 1u || L > 4u) return -1;
        if (L > 1u && p[0] == 0u) return -1; /* not minimal */
        v.v.u = get_be(p, L);
        break;
    case BAC_TAG_SIGNED: {
        uint32_t u;
        if (L < 1u || L > 4u) return -1;
        if (L > 1u && ((p[0] == 0x00u && !(p[1] & 0x80u)) || (p[0] == 0xFFu && (p[1] & 0x80u))))
            return -1; /* not minimal */
        u = get_be(p, L);
        if (L < 4u && (p[0] & 0x80u)) u |= 0xFFFFFFFFul << (8u * L); /* sign-extend */
        v.v.i = (u > 0x7FFFFFFFul) ? (int32_t)(-(int32_t)(~u) - 1) : (int32_t)u;
        break;
    }
    case BAC_TAG_REAL: {
        uint32_t bits;
        if (L != 4u) return -1;
        bits = get_be(p, 4);
        memcpy(&v.v.r, &bits, sizeof bits);
        break;
    }
    case BAC_TAG_OCTET_STRING:
        v.v.octets.data = p;
        v.v.octets.len = L;
        break;
    case BAC_TAG_CHARACTER_STRING:
        if (L < 1u) return -1; /* character-set octet is mandatory */
        v.v.str.charset = p[0];
        v.v.str.data = p + 1;
        v.v.str.len = L - 1u;
        if (p[0] == 0u && !utf8_valid(p + 1, L - 1u)) return -1;
        break;
    case BAC_TAG_BIT_STRING:
        if (L < 1u || p[0] > 7u) return -1;
        if (L == 1u && p[0] != 0u) return -1; /* empty string: unused count must be 0 */
        if (L - 1u > SIZE_MAX / 8u) return -1;
        v.v.bits.data = p + 1;
        v.v.bits.nbits = (L - 1u) * 8u - p[0];
        break;
    case BAC_TAG_DATE:
        if (L != 4u || !date_fields_valid(p[1], p[2], p[3])) return -1;
        v.v.date.year = (p[0] == UNSPEC) ? BAC_YEAR_UNSPECIFIED : (uint16_t)(DATE_YEAR_MIN + p[0]);
        v.v.date.month = p[1];
        v.v.date.day = p[2];
        v.v.date.wday = p[3];
        break;
    case BAC_TAG_TIME:
        if (L != 4u || !time_fields_valid(p[0], p[1], p[2], p[3])) return -1;
        v.v.time.hour = p[0];
        v.v.time.minute = p[1];
        v.v.time.second = p[2];
        v.v.time.hundredths = p[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        uint32_t u;
        if (L != 4u) return -1;
        u = get_be(p, 4);
        v.v.oid.type = (uint16_t)(u >> 22);
        v.v.oid.instance = u & 0x3FFFFFul;
        break;
    }
    default: /* DOUBLE (not representable in bac_value_t), reserved tags 13+ */
        return -1;
    }

    *out = v;
    return (int)(hl + L);
}

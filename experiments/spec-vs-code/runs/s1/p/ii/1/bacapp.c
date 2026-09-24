/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Bare-metal friendly: C11, no heap, no stdio. Only memcpy/memmove/memset are
 * used from the C library (a freestanding GCC/Clang toolchain provides them).
 *
 * Behaviour summary (the header documents the API; these are the policy
 * decisions that the header leaves open):
 *
 *  Encoders
 *   - Always produce the canonical form: shortest tag header, extended tag
 *     number only for tags 15..254, integers in the fewest octets.
 *   - Validate every argument against the value domain defined in clause 20.2
 *     and return -1 for anything outside it (tag 255, object type > 1023,
 *     instance > 4194303, year outside 1900..2154, month 0, 30 February,
 *     hour 24, invalid UTF-8, ...).  Wildcards (255, 0xFFFF year) and the
 *     special date values (month 13/14, day 32/33/34) are accepted.
 *   - Never write to buf when returning -1, never write past cap, and never
 *     depend on the previous contents of buf.
 *   - bits[i] != 0 means bit i is set.
 *
 *  Decoders
 *   - Accept any header or integer form whose meaning is unambiguous, even if
 *     not the shortest (e.g. extended length used for a short value, leading
 *     zero octets in an Unsigned), provided the value fits the output type.
 *   - Reject encodings that are malformed or outside the value domain:
 *     truncated input, application-class LVT 6/7, extended tag octet 255,
 *     Boolean LVT > 1, zero-length integers, wrong fixed lengths, bit-string
 *     unused count > 7, invalid UTF-8 (charset 0), reserved charsets,
 *     out-of-range date/time fields.
 *   - Double (tag 5) and the reserved tags 13+ have no representation in
 *     bac_value_t and are rejected; bac_dec_tag() can be used to skip them.
 *   - *out is only written when the call succeeds.
 */
#include "bacapp.h"

#include <limits.h>
#include <string.h>

/* ---- tag octet layout (20.2.1) ------------------------------------------- */
#define CLASS_CONTEXT     0x08u /* class bit of the initial octet */
#define LVT_MASK          0x07u
#define LVT_EXTENDED      5u    /* length follows in 1, 3 or 5 octets */
#define LVT_OPENING       6u
#define LVT_CLOSING       7u
#define TAGNUM_EXTENDED   15u   /* tag number follows in the next octet */
#define TAG_RESERVED      255u  /* extended tag octet value reserved by ASHRAE */
#define EXT_LEN_16        254u  /* extended length: 2 more octets */
#define EXT_LEN_32        255u  /* extended length: 4 more octets */

/* ---- value domains -------------------------------------------------------- */
#define UNSPECIFIED       255u
#define YEAR_BASE         1900u
#define YEAR_MAX          (YEAR_BASE + 254u)
#define OID_TYPE_MAX      0x3FFu     /* 10 bits */
#define OID_INSTANCE_MAX  0x3FFFFFu  /* 22 bits */

enum { CHARSET_UTF8 = 0, CHARSET_DBCS = 1, CHARSET_JIS = 2, CHARSET_UCS4 = 3,
       CHARSET_UCS2 = 4, CHARSET_8859_1 = 5 };

/* ========================================================================== */
/* Encoding helpers                                                            */
/* ========================================================================== */

/* Octets needed for a tag header with tag number `tag` and length `len`. */
static size_t hdr_size(uint8_t tag, uint32_t len)
{
    size_t n = (tag >= TAGNUM_EXTENDED) ? 2u : 1u;
    if (len <= 4u) return n;
    if (len <= 253u) return n + 1u;
    if (len <= 65535u) return n + 3u;
    return n + 5u;
}

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    while (n > 0u) {
        n--;
        p[n] = (uint8_t)(v & 0xFFu);
        v >>= 8;
    }
}

/* Initial octet (and extended tag octet) with the given raw LVT field.
 * Returns the number of octets written (1 or 2). */
static size_t put_tag_octets(uint8_t *buf, uint8_t tag, bool ctx, uint8_t lvt_field)
{
    uint8_t tn = (tag >= TAGNUM_EXTENDED) ? (uint8_t)TAGNUM_EXTENDED : tag;
    buf[0] = (uint8_t)((unsigned)(tn << 4) | (ctx ? CLASS_CONTEXT : 0u) | lvt_field);
    if (tag >= TAGNUM_EXTENDED) {
        buf[1] = tag;
        return 2u;
    }
    return 1u;
}

/* Tag header whose LVT is a length. Returns the header size. */
static size_t put_hdr(uint8_t *buf, uint8_t tag, bool ctx, uint32_t len)
{
    size_t n = put_tag_octets(buf, tag, ctx, (uint8_t)(len <= 4u ? len : LVT_EXTENDED));
    if (len > 4u) {
        if (len <= 253u) {
            buf[n++] = (uint8_t)len;
        } else if (len <= 65535u) {
            buf[n++] = (uint8_t)EXT_LEN_16;
            put_be(buf + n, len, 2u);
            n += 2u;
        } else {
            buf[n++] = (uint8_t)EXT_LEN_32;
            put_be(buf + n, len, 4u);
            n += 4u;
        }
    }
    return n;
}

/* Check that a value with tag number `tag` and `clen` content octets can be
 * encoded into buf[0..cap). Writes nothing. Returns the total size, or -1.
 * *hl receives the header size. */
static int reserve(const uint8_t *buf, size_t cap, uint8_t tag, size_t clen, size_t *hl)
{
    if (buf == NULL || tag == TAG_RESERVED) return -1;
    if (clen > (size_t)INT_MAX) return -1;            /* also bounds clen to 32 bits */
    *hl = hdr_size(tag, (uint32_t)clen);
    if (clen > (size_t)INT_MAX - *hl) return -1;      /* result must fit the int return */
    if (*hl + clen > cap) return -1;
    return (int)(*hl + clen);
}

/* Fewest octets for an unsigned value (20.2.4). */
static size_t unsigned_len(uint32_t v)
{
    if (v <= 0xFFu) return 1u;
    if (v <= 0xFFFFu) return 2u;
    if (v <= 0xFFFFFFu) return 3u;
    return 4u;
}

/* Fewest octets for a two's-complement value (20.2.5). */
static size_t signed_len(int32_t v)
{
    if (v >= -128 && v <= 127) return 1u;
    if (v >= -32768 && v <= 32767) return 2u;
    if (v >= -8388608L && v <= 8388607L) return 3u;
    return 4u;
}

/* Tagged integer (unsigned, enumerated or context unsigned). */
static int enc_uint(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v)
{
    size_t hl, clen = unsigned_len(v);
    int n = reserve(buf, cap, tag, clen, &hl);
    if (n < 0) return -1;
    put_hdr(buf, tag, ctx, (uint32_t)clen);
    put_be(buf + hl, v, clen);
    return n;
}

/* Tagged fixed 4-octet content (date, time, object id, real). */
static int enc_four(uint8_t *buf, size_t cap, uint8_t tag, const uint8_t content[4])
{
    size_t hl;
    int n = reserve(buf, cap, tag, 4u, &hl);
    if (n < 0) return -1;
    put_hdr(buf, tag, false, 4u);
    memcpy(buf + hl, content, 4u);
    return n;
}

/* RFC 3629 UTF-8 validation: rejects overlong forms, surrogates, > U+10FFFF. */
static bool utf8_valid(const uint8_t *s, size_t n)
{
    size_t i = 0;
    while (i < n) {
        uint8_t b = s[i];
        size_t need, k;
        uint8_t lo = 0x80u, hi = 0xBFu; /* allowed range of the 2nd octet */
        if (b < 0x80u) { i++; continue; }
        if (b >= 0xC2u && b <= 0xDFu) need = 1u;
        else if (b == 0xE0u) { need = 2u; lo = 0xA0u; }
        else if (b >= 0xE1u && b <= 0xECu) need = 2u;
        else if (b == 0xEDu) { need = 2u; hi = 0x9Fu; }
        else if (b == 0xEEu || b == 0xEFu) need = 2u;
        else if (b == 0xF0u) { need = 3u; lo = 0x90u; }
        else if (b >= 0xF1u && b <= 0xF3u) need = 3u;
        else if (b == 0xF4u) { need = 3u; hi = 0x8Fu; }
        else return false;
        if (n - i - 1u < need) return false;
        if (s[i + 1u] < lo || s[i + 1u] > hi) return false;
        for (k = 2u; k <= need; k++)
            if ((s[i + k] & 0xC0u) != 0x80u) return false;
        i += need + 1u;
    }
    return true;
}

static bool is_leap(unsigned year)
{
    return (year % 4u == 0u && year % 100u != 0u) || year % 400u == 0u;
}

/* Date fields per 20.2.12. year_known: year holds a calendar year. */
static bool date_valid(bool year_known, unsigned year, uint8_t month, uint8_t day, uint8_t wday)
{
    static const uint8_t mdays[12] = { 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 };
    if (!((month >= 1u && month <= 14u) || month == UNSPECIFIED)) return false; /* 13 odd, 14 even */
    if (!((day >= 1u && day <= 34u) || day == UNSPECIFIED)) return false;       /* 32 last, 33 odd, 34 even */
    if (!((wday >= 1u && wday <= 7u) || wday == UNSPECIFIED)) return false;     /* 1 = Monday */
    if (month <= 12u && day <= 31u) {
        unsigned max = mdays[month - 1u];
        if (month == 2u && year_known && !is_leap(year)) max = 28u;
        if (day > max) return false;
    }
    return true;
}

/* Time fields per 20.2.13. */
static bool time_valid(uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    return (hour <= 23u || hour == UNSPECIFIED) &&
           (minute <= 59u || minute == UNSPECIFIED) &&
           (second <= 59u || second == UNSPECIFIED) &&
           (hundredths <= 99u || hundredths == UNSPECIFIED);
}

/* ========================================================================== */
/* Application-tagged encoders                                                 */
/* ========================================================================== */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t hl;
    int n = reserve(buf, cap, BAC_TAG_NULL, 0u, &hl);
    if (n < 0) return -1;
    put_hdr(buf, BAC_TAG_NULL, false, 0u);
    return n;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    size_t hl;
    int n = reserve(buf, cap, BAC_TAG_BOOLEAN, 0u, &hl);
    if (n < 0) return -1;
    /* 20.2.3: the value is carried in the LVT field, no content octets */
    put_tag_octets(buf, BAC_TAG_BOOLEAN, false, v ? 1u : 0u);
    return n;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    size_t hl, clen = signed_len(v);
    int n = reserve(buf, cap, BAC_TAG_SIGNED, clen, &hl);
    if (n < 0) return -1;
    put_hdr(buf, BAC_TAG_SIGNED, false, (uint32_t)clen);
    put_be(buf + hl, (uint32_t)v, clen); /* conversion to uint32_t is modulo 2^32 */
    return n;
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t c[4];
    memcpy(&bits, &v, sizeof bits); /* IEEE-754 single, all patterns incl. NaN/Inf */
    put_be(c, bits, 4u);
    return enc_four(buf, cap, BAC_TAG_REAL, c);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    size_t hl;
    int n;
    if (data == NULL && len > 0u) return -1;
    n = reserve(buf, cap, BAC_TAG_OCTET_STRING, len, &hl);
    if (n < 0) return -1;
    if (len > 0u) memmove(buf + hl, data, len); /* content first: data may alias buf */
    put_hdr(buf, BAC_TAG_OCTET_STRING, false, (uint32_t)len);
    return n;
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    size_t hl;
    int n;
    if (utf8 == NULL && len > 0u) return -1;
    if (len >= (size_t)INT_MAX) return -1; /* charset octet + text must fit */
    if (len > 0u && !utf8_valid((const uint8_t *)utf8, len)) return -1;
    n = reserve(buf, cap, BAC_TAG_CHARACTER_STRING, len + 1u, &hl);
    if (n < 0) return -1;
    if (len > 0u) memmove(buf + hl + 1u, utf8, len);
    put_hdr(buf, BAC_TAG_CHARACTER_STRING, false, (uint32_t)(len + 1u));
    buf[hl] = CHARSET_UTF8;
    return n;
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t hl, i, octets, clen;
    int n;
    if (bits == NULL && nbits > 0u) return -1;
    octets = nbits / 8u + (nbits % 8u != 0u ? 1u : 0u);
    clen = octets + 1u; /* cannot overflow: octets <= SIZE_MAX / 8 + 1 */
    n = reserve(buf, cap, BAC_TAG_BIT_STRING, clen, &hl);
    if (n < 0) return -1;
    put_hdr(buf, BAC_TAG_BIT_STRING, false, (uint32_t)clen);
    buf[hl] = (uint8_t)(octets * 8u - nbits); /* unused bits in the final octet */
    memset(buf + hl + 1u, 0, octets);          /* unused bits are sent as 0 */
    for (i = 0; i < nbits; i++)
        if (bits[i] != 0u)
            buf[hl + 1u + i / 8u] |= (uint8_t)(0x80u >> (i % 8u)); /* bit 0 = MSB */
    return n;
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t c[4];
    bool known = (year != BAC_YEAR_UNSPECIFIED);
    if (known && (year < YEAR_BASE || year > YEAR_MAX)) return -1;
    if (!date_valid(known, year, month, day, wday)) return -1;
    c[0] = known ? (uint8_t)(year - YEAR_BASE) : (uint8_t)UNSPECIFIED;
    c[1] = month;
    c[2] = day;
    c[3] = wday;
    return enc_four(buf, cap, BAC_TAG_DATE, c);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    uint8_t c[4];
    if (!time_valid(hour, minute, second, hundredths)) return -1;
    c[0] = hour;
    c[1] = minute;
    c[2] = second;
    c[3] = hundredths;
    return enc_four(buf, cap, BAC_TAG_TIME, c);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > OID_TYPE_MAX || instance > OID_INSTANCE_MAX) return -1;
    put_be(c, ((uint32_t)type << 22) | instance, 4u);
    return enc_four(buf, cap, BAC_TAG_OBJECT_ID, c);
}

/* ========================================================================== */
/* Context-tagged encoders                                                     */
/* ========================================================================== */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_uint(buf, cap, tag, true, v);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* 20.2.3: context-tagged Boolean has one content octet */
    size_t hl;
    int n = reserve(buf, cap, tag, 1u, &hl);
    if (n < 0) return -1;
    put_hdr(buf, tag, true, 1u);
    buf[hl] = v ? 1u : 0u;
    return n;
}

static int enc_construct(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt_field)
{
    size_t hl;
    int n = reserve(buf, cap, tag, 0u, &hl);
    if (n < 0) return -1;
    put_tag_octets(buf, tag, true, lvt_field);
    return n;
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_construct(buf, cap, tag, LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_construct(buf, cap, tag, LVT_CLOSING);
}

/* ========================================================================== */
/* Decoders                                                                    */
/* ========================================================================== */

typedef struct {
    uint8_t tag;
    bool ctx;
    uint8_t lvt_field; /* raw 3-bit LVT field of the initial octet */
    uint32_t lvt;      /* length (or Boolean value) after extension */
    size_t hl;         /* header length in octets */
} hdr_t;

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    size_t i;
    for (i = 0; i < n; i++) v = (v << 8) | p[i];
    return v;
}

static bool parse_hdr(const uint8_t *buf, size_t len, hdr_t *h)
{
    size_t n = 1u;
    if (buf == NULL || len < 1u) return false;
    h->ctx = (buf[0] & CLASS_CONTEXT) != 0u;
    h->lvt_field = (uint8_t)(buf[0] & LVT_MASK);
    h->tag = (uint8_t)(buf[0] >> 4);
    h->lvt = 0;
    if (h->tag == TAGNUM_EXTENDED) {
        if (len < 2u || buf[1] == TAG_RESERVED) return false;
        h->tag = buf[1];
        n = 2u;
    }
    if (h->ctx) {
        if (h->lvt_field == LVT_OPENING || h->lvt_field == LVT_CLOSING) {
            h->hl = n;
            return true;
        }
    } else {
        /* opening/closing tags are context class only (20.2.1.3.2) */
        if (h->lvt_field == LVT_OPENING || h->lvt_field == LVT_CLOSING) return false;
        if (h->tag == BAC_TAG_BOOLEAN) {
            /* 20.2.3: LVT is the value (0/1), never a length */
            if (h->lvt_field > 1u) return false;
            h->lvt = h->lvt_field;
            h->hl = n;
            return true;
        }
    }
    if (h->lvt_field < LVT_EXTENDED) {
        h->lvt = h->lvt_field;
    } else {
        uint8_t e;
        if (len - n < 1u) return false;
        e = buf[n++];
        if (e < EXT_LEN_16) {
            h->lvt = e;
        } else {
            size_t w = (e == EXT_LEN_16) ? 2u : 4u;
            if (len - n < w) return false;
            h->lvt = get_be(buf + n, w);
            n += w;
        }
    }
    h->hl = n;
    return true;
}

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    hdr_t h;
    if (out == NULL || !parse_hdr(buf, len, &h)) return -1;
    out->tag = h.tag;
    out->context = h.ctx;
    out->opening = h.ctx && h.lvt_field == LVT_OPENING;
    out->closing = h.ctx && h.lvt_field == LVT_CLOSING;
    out->lvt = h.lvt;
    return (int)h.hl;
}

/* Integer content: at least one octet; extra leading octets are accepted only
 * if they are pure zero/sign extension so the value still fits 32 bits. */
static bool dec_unsigned(const uint8_t *c, size_t clen, uint32_t *v)
{
    size_t i;
    if (clen < 1u) return false;
    for (i = 0; i + 4u < clen; i++)
        if (c[i] != 0u) return false;
    *v = get_be(c + i, clen - i);
    return true;
}

static bool dec_signed(const uint8_t *c, size_t clen, int32_t *v)
{
    size_t i;
    uint32_t u;
    if (clen < 1u) return false;
    if (clen > 4u) {
        uint8_t ext = (c[clen - 4u] & 0x80u) ? 0xFFu : 0x00u;
        for (i = 0; i + 4u < clen; i++)
            if (c[i] != ext) return false;
        c += clen - 4u;
        clen = 4u;
    }
    u = (c[0] & 0x80u) ? 0xFFFFFFFFu : 0u;
    for (i = 0; i < clen; i++) u = (u << 8) | c[i];
    /* two's complement to int32_t without implementation-defined conversion */
    *v = (u <= (uint32_t)INT32_MAX) ? (int32_t)u : (int32_t)(-(int32_t)(~u) - 1);
    return true;
}

static bool charset_content_valid(uint8_t cs, const uint8_t *s, size_t n)
{
    switch (cs) {
    case CHARSET_UTF8:   return utf8_valid(s, n);
    case CHARSET_DBCS:   return n >= 2u;         /* 2-octet code page follows */
    case CHARSET_JIS:    return true;
    case CHARSET_UCS4:   return n % 4u == 0u;
    case CHARSET_UCS2:   return n % 2u == 0u;
    case CHARSET_8859_1: return true;
    default:             return false;           /* reserved by ASHRAE */
    }
}

static bool dec_content(uint8_t tag, const uint8_t *c, size_t clen, bac_value_t *v)
{
    switch (tag) {
    case BAC_TAG_NULL:
        return clen == 0u;
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        return dec_unsigned(c, clen, &v->v.u);
    case BAC_TAG_SIGNED:
        return dec_signed(c, clen, &v->v.i);
    case BAC_TAG_REAL: {
        uint32_t bits;
        if (clen != 4u) return false;
        bits = get_be(c, 4u);
        memcpy(&v->v.r, &bits, sizeof bits);
        return true;
    }
    case BAC_TAG_OCTET_STRING:
        v->v.octets.data = c;
        v->v.octets.len = clen;
        return true;
    case BAC_TAG_CHARACTER_STRING:
        if (clen < 1u || !charset_content_valid(c[0], c + 1, clen - 1u)) return false;
        v->v.str.charset = c[0];
        v->v.str.data = c + 1;
        v->v.str.len = clen - 1u;
        return true;
    case BAC_TAG_BIT_STRING:
        if (clen < 1u || c[0] > 7u) return false;
        if (clen == 1u && c[0] != 0u) return false;   /* empty string has no unused bits */
        if (clen - 1u > SIZE_MAX / 8u) return false;
        v->v.bits.data = c + 1;
        v->v.bits.nbits = (clen - 1u) * 8u - c[0];
        return true;
    case BAC_TAG_DATE: {
        bool known;
        unsigned year;
        if (clen != 4u) return false;
        known = (c[0] != UNSPECIFIED);
        year = YEAR_BASE + c[0];
        if (!date_valid(known, year, c[1], c[2], c[3])) return false;
        v->v.date.year = known ? (uint16_t)year : (uint16_t)BAC_YEAR_UNSPECIFIED;
        v->v.date.month = c[1];
        v->v.date.day = c[2];
        v->v.date.wday = c[3];
        return true;
    }
    case BAC_TAG_TIME:
        if (clen != 4u || !time_valid(c[0], c[1], c[2], c[3])) return false;
        v->v.time.hour = c[0];
        v->v.time.minute = c[1];
        v->v.time.second = c[2];
        v->v.time.hundredths = c[3];
        return true;
    case BAC_TAG_OBJECT_ID: {
        uint32_t raw;
        if (clen != 4u) return false;
        raw = get_be(c, 4u);
        v->v.oid.type = (uint16_t)(raw >> 22);
        v->v.oid.instance = raw & OID_INSTANCE_MAX;
        return true;
    }
    default: /* DOUBLE: no field in bac_value_t; 13+: reserved */
        return false;
    }
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    hdr_t h;
    bac_value_t v;
    size_t total;
    if (out == NULL || !parse_hdr(buf, len, &h) || h.ctx) return -1;
    memset(&v, 0, sizeof v);
    v.tag = h.tag;
    if (h.tag == BAC_TAG_BOOLEAN) {
        v.v.boolean = (h.lvt != 0u);
        total = h.hl;
    } else {
        if ((uint64_t)h.lvt > (uint64_t)(len - h.hl)) return -1; /* truncated */
        if (!dec_content(h.tag, buf + h.hl, (size_t)h.lvt, &v)) return -1;
        total = h.hl + (size_t)h.lvt;
    }
    if (total > (size_t)INT_MAX) return -1;
    *out = v;
    return (int)total;
}

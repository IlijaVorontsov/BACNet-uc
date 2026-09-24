/* driver.c - line-oriented test CLI for bacapp (test infrastructure; do not modify).
 *
 * Reads commands from stdin, prints exactly one result line per command.
 * Byte strings are hex; segments may be joined with '+', and 'rep:HH:N' means
 * N copies of byte HH (e.g. 7505+00+rep:41:4). '-' is the empty string.
 * Encoder commands accept an optional last argument '@N' = buffer capacity
 * (default 1024).  Encoder results:  OK <hex>  |  ERR  |  ERR DIRTY (buffer
 * modified although -1 returned)  |  ... OVERRUN (wrote past cap).
 * Long byte strings are printed as <first 16 bytes>..<fnv1a64>/<length>.
 */
#include "bacapp.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define GUARD 16
#define MAXCAP 80000
static uint8_t obuf[MAXCAP + GUARD];
static uint8_t obuf2[MAXCAP + GUARD];
static uint8_t data[MAXCAP];

static int hexval(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* parse a byte-string argument into dst; returns length or -1 */
static long parse_bytes(const char *s, uint8_t *dst, size_t max) {
    size_t n = 0;
    if (strcmp(s, "-") == 0) return 0;
    while (*s) {
        if (strncmp(s, "rep:", 4) == 0) {
            int hi = hexval(s[4]), lo = hexval(s[5]);
            if (hi < 0 || lo < 0 || s[6] != ':') return -1;
            char *end;
            unsigned long cnt = strtoul(s + 7, &end, 10);
            if (n + cnt > max) return -1;
            memset(dst + n, hi * 16 + lo, cnt);
            n += cnt;
            s = end;
        } else {
            while (*s && *s != '+') {
                int hi = hexval(s[0]), lo = s[1] ? hexval(s[1]) : -1;
                if (hi < 0 || lo < 0 || n >= max) return -1;
                dst[n++] = (uint8_t)(hi * 16 + lo);
                s += 2;
            }
        }
        if (*s == '+') s++;
        else if (*s) return -1;
    }
    return (long)n;
}

/* bit-string argument: digits (each digit = value of one bit), '+' / rep:D:N segments */
static long parse_bits(const char *s, uint8_t *dst, size_t max) {
    size_t n = 0;
    if (strcmp(s, "-") == 0) return 0;
    while (*s) {
        if (strncmp(s, "rep:", 4) == 0) {
            int d = s[4] - '0';
            if (d < 0 || d > 9 || s[5] != ':') return -1;
            char *end;
            unsigned long cnt = strtoul(s + 6, &end, 10);
            if (n + cnt > max) return -1;
            memset(dst + n, d, cnt);
            n += cnt;
            s = end;
        } else {
            while (*s && *s != '+') {
                if (*s < '0' || *s > '9' || n >= max) return -1;
                dst[n++] = (uint8_t)(*s - '0');
                s++;
            }
        }
        if (*s == '+') s++;
        else if (*s) return -1;
    }
    return (long)n;
}

static unsigned long long fnv1a(const uint8_t *p, size_t n) {
    unsigned long long h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i++) { h ^= p[i]; h *= 1099511628211ULL; }
    return h;
}

static void print_bytes(const uint8_t *p, size_t n) {
    if (n == 0) { printf("-"); return; }
    if (n <= 64) { for (size_t i = 0; i < n; i++) printf("%02x", p[i]); return; }
    for (size_t i = 0; i < 16; i++) printf("%02x", p[i]);
    printf("..%016llx/%zu", fnv1a(p, n), n);
}

static void print_bitdigits(const uint8_t *p, size_t nbits) {
    if (nbits == 0) { printf("-"); return; }
    if (nbits <= 256) {
        for (size_t i = 0; i < nbits; i++) putchar((p[i / 8] >> (7 - i % 8)) & 1 ? '1' : '0');
        return;
    }
    size_t nb = (nbits + 7) / 8;
    printf("%zubits..%016llx", nbits, fnv1a(p, nb - 1) ^ (unsigned long long)(p[nb - 1] >> (nb * 8 - nbits)));
}

static int argc_;
static char *argv_[8];
static size_t cap_;

static int u64arg(int i, unsigned long long max, unsigned long long *out) {
    if (i >= argc_) return 0;
    char *end;
    *out = strtoull(argv_[i], &end, 10);
    return *end == 0 && argv_[i][0] != '-' && *out <= max;
}
static int s64arg(int i, long long min, long long max, long long *out) {
    if (i >= argc_) return 0;
    char *end;
    *out = strtoll(argv_[i], &end, 10);
    return *end == 0 && *out >= min && *out <= max;
}
static int hexarg(int i, int nbytes, unsigned long long *out) {
    if (i >= argc_ || (int)strlen(argv_[i]) != nbytes * 2) return 0;
    char *end;
    *out = strtoull(argv_[i], &end, 16);
    return *end == 0;
}

typedef int (*encfn)(uint8_t *buf, size_t cap, const void *ctx);

/* Run an encoder twice with different sentinel fills and report. */
#define RUN_ENC(CALL)                                                             \
    do {                                                                          \
        memset(obuf, 0xA5, cap_ + GUARD);                                         \
        uint8_t *buf = obuf;                                                      \
        int r1 = (CALL);                                                          \
        memcpy(obuf2, obuf, cap_ + GUARD);                                        \
        memset(obuf, 0x5A, cap_ + GUARD);                                         \
        int r2 = (CALL);                                                          \
        report(r1, r2);                                                           \
    } while (0)

static void report(int r1, int r2) {
    /* obuf2 = result of run 1 (0xA5 fill), obuf = result of run 2 (0x5A fill) */
    if (r1 != r2) { printf("NONDET"); return; }
    if (r1 < 0) {
        int dirty = 0;
        for (size_t i = 0; i < cap_ + GUARD; i++)
            if (obuf2[i] != 0xA5 || obuf[i] != 0x5A) dirty = 1;
        printf(dirty ? "ERR DIRTY" : "ERR");
        return;
    }
    if ((size_t)r1 > cap_) { printf("BADLEN %d", r1); return; }
    int over = 0;
    for (size_t i = cap_; i < cap_ + GUARD; i++)
        if (obuf2[i] != 0xA5 || obuf[i] != 0x5A) over = 1;
    if (memcmp(obuf, obuf2, (size_t)r1) != 0) { printf("NONDET"); return; }
    printf("OK ");
    print_bytes(obuf, (size_t)r1);
    if (over) printf(" OVERRUN");
}

static void do_dec_tag(const uint8_t *in, size_t n) {
    uint8_t *copy = malloc(n ? n : 1);
    memcpy(copy, in, n);
    bac_tag_t t;
    memset(&t, 0, sizeof t);
    int r = bac_dec_tag(copy, n, &t);
    if (r < 0) printf("ERR");
    else {
        printf("OK hl=%d tag=%u %s", r, t.tag, t.context ? "ctx" : "app");
        if (t.opening) printf(" open");
        if (t.closing) printf(" close");
        if (!t.opening && !t.closing) printf(" lvt=%u", (unsigned)t.lvt);
    }
    free(copy);
}

static void do_dec_app(const uint8_t *in, size_t n) {
    uint8_t *copy = malloc(n ? n : 1);
    memcpy(copy, in, n);
    bac_value_t v;
    memset(&v, 0, sizeof v);
    int r = bac_dec_app(copy, n, &v);
    if (r < 0) { printf("ERR"); free(copy); return; }
    printf("OK n=%d ", r);
    switch (v.tag) {
    case BAC_TAG_NULL: printf("null"); break;
    case BAC_TAG_BOOLEAN: printf("bool %d", v.v.boolean ? 1 : 0); break;
    case BAC_TAG_UNSIGNED: printf("u %u", (unsigned)v.v.u); break;
    case BAC_TAG_SIGNED: printf("i %d", (int)v.v.i); break;
    case BAC_TAG_REAL: { uint32_t b; memcpy(&b, &v.v.r, 4); printf("r %08x", (unsigned)b); break; }
/*V11*/    case BAC_TAG_DOUBLE: { uint64_t b; memcpy(&b, &v.v.d, 8); printf("d %016llx", (unsigned long long)b); break; }
    case BAC_TAG_OCTET_STRING: printf("oct "); print_bytes(v.v.octets.data, v.v.octets.len); break;
    case BAC_TAG_CHARACTER_STRING: printf("str cs=%u ", v.v.str.charset); print_bytes(v.v.str.data, v.v.str.len); break;
    case BAC_TAG_BIT_STRING: printf("bits "); print_bitdigits(v.v.bits.data, v.v.bits.nbits); break;
    case BAC_TAG_ENUMERATED: printf("enum %u", (unsigned)v.v.u); break;
    case BAC_TAG_DATE: printf("date %u %u %u %u", v.v.date.year, v.v.date.month, v.v.date.day, v.v.date.wday); break;
    case BAC_TAG_TIME: printf("time %u %u %u %u", v.v.time.hour, v.v.time.minute, v.v.time.second, v.v.time.hundredths); break;
    case BAC_TAG_OBJECT_ID: printf("oid %u %u", v.v.oid.type, (unsigned)v.v.oid.instance); break;
    default: printf("tag%u ?", v.tag); break;
    }
    free(copy);
}

static void command(char *line) {
    argc_ = 0;
    for (char *tok = strtok(line, " \t"); tok && argc_ < 8; tok = strtok(NULL, " \t")) argv_[argc_++] = tok;
    if (argc_ == 0) { printf("BADCMD"); return; }
    cap_ = 1024;
    if (argc_ > 1 && argv_[argc_ - 1][0] == '@') {
        cap_ = strtoul(argv_[argc_ - 1] + 1, NULL, 10);
        if (cap_ > MAXCAP) { printf("BADCMD"); return; }
        argc_--;
    }
    const char *c = argv_[0];
    unsigned long long a, b, d, e;
    long long s;
    long n;
    if (!strcmp(c, "enc_null") && argc_ == 1) { RUN_ENC(bac_enc_null(buf, cap_)); return; }
    if (!strcmp(c, "enc_bool") && argc_ == 2 && u64arg(1, 1, &a)) { RUN_ENC(bac_enc_boolean(buf, cap_, a != 0)); return; }
    if (!strcmp(c, "enc_unsigned") && argc_ == 2 && u64arg(1, 0xFFFFFFFFull, &a)) { RUN_ENC(bac_enc_unsigned(buf, cap_, (uint32_t)a)); return; }
    if (!strcmp(c, "enc_enum") && argc_ == 2 && u64arg(1, 0xFFFFFFFFull, &a)) { RUN_ENC(bac_enc_enumerated(buf, cap_, (uint32_t)a)); return; }
    if (!strcmp(c, "enc_signed") && argc_ == 2 && s64arg(1, -2147483648LL, 2147483647LL, &s)) { RUN_ENC(bac_enc_signed(buf, cap_, (int32_t)s)); return; }
    if (!strcmp(c, "enc_real") && argc_ == 2 && hexarg(1, 4, &a)) {
        uint32_t bits = (uint32_t)a; float f; memcpy(&f, &bits, 4);
        RUN_ENC(bac_enc_real(buf, cap_, f)); return;
    }
/*V11*/    if (!strcmp(c, "enc_double") && argc_ == 2 && hexarg(1, 8, &a)) {
/*V11*/        uint64_t bits = (uint64_t)a; double f; memcpy(&f, &bits, 8);
/*V11*/        RUN_ENC(bac_enc_double(buf, cap_, f)); return;
/*V11*/    }
    if (!strcmp(c, "enc_octets") && argc_ == 2 && (n = parse_bytes(argv_[1], data, sizeof data)) >= 0) { RUN_ENC(bac_enc_octet_string(buf, cap_, data, (size_t)n)); return; }
    if (!strcmp(c, "enc_str") && argc_ == 2 && (n = parse_bytes(argv_[1], data, sizeof data)) >= 0) { RUN_ENC(bac_enc_char_string(buf, cap_, (const char *)data, (size_t)n)); return; }
    if (!strcmp(c, "enc_bits") && argc_ == 2 && (n = parse_bits(argv_[1], data, sizeof data)) >= 0) { RUN_ENC(bac_enc_bit_string(buf, cap_, data, (size_t)n)); return; }
    if (!strcmp(c, "enc_date") && argc_ == 5 && u64arg(1, 65535, &a) && u64arg(2, 255, &b) && u64arg(3, 255, &d) && u64arg(4, 255, &e)) {
        RUN_ENC(bac_enc_date(buf, cap_, (uint16_t)a, (uint8_t)b, (uint8_t)d, (uint8_t)e)); return;
    }
    if (!strcmp(c, "enc_time") && argc_ == 5 && u64arg(1, 255, &a) && u64arg(2, 255, &b) && u64arg(3, 255, &d) && u64arg(4, 255, &e)) {
        RUN_ENC(bac_enc_time(buf, cap_, (uint8_t)a, (uint8_t)b, (uint8_t)d, (uint8_t)e)); return;
    }
    if (!strcmp(c, "enc_oid") && argc_ == 3 && u64arg(1, 65535, &a) && u64arg(2, 0xFFFFFFFFull, &b)) { RUN_ENC(bac_enc_object_id(buf, cap_, (uint16_t)a, (uint32_t)b)); return; }
    if (!strcmp(c, "enc_ctx_unsigned") && argc_ == 3 && u64arg(1, 255, &a) && u64arg(2, 0xFFFFFFFFull, &b)) { RUN_ENC(bac_enc_ctx_unsigned(buf, cap_, (uint8_t)a, (uint32_t)b)); return; }
/*V11*/    if (!strcmp(c, "enc_ctx_enum") && argc_ == 3 && u64arg(1, 255, &a) && u64arg(2, 0xFFFFFFFFull, &b)) { RUN_ENC(bac_enc_ctx_enumerated(buf, cap_, (uint8_t)a, (uint32_t)b)); return; }
/*V11*/    if (!strcmp(c, "enc_ctx_signed") && argc_ == 3 && u64arg(1, 255, &a) && s64arg(2, -2147483648LL, 2147483647LL, &s)) { RUN_ENC(bac_enc_ctx_signed(buf, cap_, (uint8_t)a, (int32_t)s)); return; }
    if (!strcmp(c, "enc_ctx_bool") && argc_ == 3 && u64arg(1, 255, &a) && u64arg(2, 1, &b)) { RUN_ENC(bac_enc_ctx_boolean(buf, cap_, (uint8_t)a, b != 0)); return; }
    if (!strcmp(c, "enc_open") && argc_ == 2 && u64arg(1, 255, &a)) { RUN_ENC(bac_enc_opening_tag(buf, cap_, (uint8_t)a)); return; }
    if (!strcmp(c, "enc_close") && argc_ == 2 && u64arg(1, 255, &a)) { RUN_ENC(bac_enc_closing_tag(buf, cap_, (uint8_t)a)); return; }
    if (!strcmp(c, "dec_tag") && argc_ == 2 && (n = parse_bytes(argv_[1], data, sizeof data)) >= 0) { do_dec_tag(data, (size_t)n); return; }
    if (!strcmp(c, "dec_app") && argc_ == 2 && (n = parse_bytes(argv_[1], data, sizeof data)) >= 0) { do_dec_app(data, (size_t)n); return; }
    printf("BADCMD");
}

int main(void) {
    static char line[400000];
    while (fgets(line, sizeof line, stdin)) {
        size_t l = strlen(line);
        while (l && (line[l - 1] == '\n' || line[l - 1] == '\r')) line[--l] = 0;
        if (l == 0) continue;
        if (!strncmp(line, "@block", 6)) { printf("%s\n", line); fflush(stdout); continue; }
        command(line);
        printf("\n");
        fflush(stdout);
    }
    return 0;
}

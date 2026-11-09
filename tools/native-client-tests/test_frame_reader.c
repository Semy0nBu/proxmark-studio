/* Execute the production header with a deterministic UART and clock.
 * No serial port, Bluetooth driver, RF operation or copied algorithm.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "frame_reader.h"

enum { OK = 0, NO_DATA = -10, TIMEOUT = -20, STOPPED = -30,
       INVALID = -40, DEAD = -50, IO_ERROR = -60 };

static unsigned long checks;
#define CHECK(x) do { ++checks; if (!(x)) { \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x); exit(1); \
} } while (0)

typedef struct {
    uint32_t bytes, delay_ms;
    int result;
    bool stop, overreport;
} event_t;

typedef struct {
    const uint8_t *source;
    uint32_t source_size, position;
    event_t events[2048];
    unsigned count, index, calls, idle_calls;
    bool event_started, run;
    uint32_t pending;
    uint64_t now;
} mock_t;

static int mock_read(void *context, uint8_t *output, uint32_t capacity, uint32_t *received) {
    mock_t *m = context;
    uint32_t take;
    event_t *event;
    ++m->calls;
    CHECK(capacity > 0);
    *received = 0;
    if (m->index >= m->count) {
        m->now += 20;
        return NO_DATA;
    }
    event = &m->events[m->index];
    if (!m->event_started) {
        m->now += event->delay_ms;
        m->pending = event->bytes;
        m->event_started = true;
    }
    take = m->pending < capacity ? m->pending : capacity;
    CHECK(m->position + take <= m->source_size);
    if (take) memcpy(output, m->source + m->position, take);
    m->position += take;
    m->pending -= take;
    *received = event->overreport ? capacity + 1 : take;
    if (event->stop) m->run = false;
    if (!m->pending || event->stop || (event->result != OK && event->result != NO_DATA)) {
        ++m->index;
        m->event_started = false;
    }
    return event->result;
}

static uint64_t mock_now(void *context) { return ((mock_t *)context)->now; }
static bool mock_running(void *context) { return ((mock_t *)context)->run; }
static void mock_idle(void *context) { ++((mock_t *)context)->idle_calls; }

static pm3_frame_reader_t reader(mock_t *m) {
    pm3_frame_reader_t r = {
        .context = m, .receive = mock_read, .now = mock_now,
        .running = mock_running, .idle = mock_idle,
        .success = OK, .no_data = NO_DATA, .timeout = TIMEOUT,
        .stopped = STOPPED, .invalid = INVALID,
        .budget_ms = PM3_FRAME_RX_BUDGET_MS,
    };
    return r;
}

static void reset(mock_t *m, const uint8_t *bytes, uint32_t length) {
    memset(m, 0, sizeof(*m)); m->source = bytes; m->source_size = length; m->run = true;
}
static void add(mock_t *m, uint32_t n, uint32_t delay, int result) {
    CHECK(m->count < sizeof(m->events) / sizeof(m->events[0]));
    m->events[m->count++] = (event_t){n, delay, result, false, false};
}
static int part(pm3_frame_reader_t *r, const uint8_t *expected, uint32_t n,
                uint32_t *received) {
    uint8_t output[1050];
    uint32_t i;
    int result;
    CHECK(n + 2 <= sizeof(output));
    memset(output, 0xcc, sizeof(output));
    result = pm3_frame_read_exact(r, output + 1, n, received);
    CHECK(*received <= n);
    CHECK(output[0] == 0xcc);
    for (i = n + 1; i < sizeof(output); ++i) CHECK(output[i] == 0xcc);
    CHECK(!memcmp(output + 1, expected, *received));
    return result;
}

static void packet(pm3_frame_reader_t *r, const uint8_t *source, uint32_t payload, bool old) {
    uint32_t n;
    CHECK(part(r, source, 10, &n) == OK && n == 10);
    if (payload) CHECK(part(r, source + 10, payload, &n) == OK && n == payload);
    if (!old) CHECK(part(r, source + 10 + payload, 2, &n) == OK && n == 2);
}

static void test_every_split_and_coalesced_bytes(const uint8_t *source) {
    static const uint32_t payloads[] = {0, 1, 2, 31, 255, 512};
    unsigned mode, p;
    for (mode = 0; mode < 2; ++mode) {
        for (p = 0; p < (mode ? 1u : sizeof(payloads) / sizeof(payloads[0])); ++p) {
            uint32_t payload = mode ? 534 : payloads[p];
            uint32_t total = 10 + payload + (mode ? 0 : 2), split;
            for (split = 1; split < total; ++split) {
                mock_t m; pm3_frame_reader_t r;
                reset(&m, source, total);
                add(&m, split, 10, OK); add(&m, total - split, 100, OK);
                r = reader(&m); packet(&r, source, payload, mode != 0);
                CHECK(m.position == total && m.now == 110);
                CHECK(r.active && r.started_ms == 0);
            }
            {
                mock_t m; pm3_frame_reader_t r;
                reset(&m, source, total); add(&m, total, 0, OK);
                r = reader(&m); packet(&r, source, payload, mode != 0);
                CHECK(m.position == total);
            }
        }
    }
}

static void test_idle_and_wakes(const uint8_t *source) {
    mock_t m; pm3_frame_reader_t r; uint32_t n;
    reset(&m, source, 10); add(&m, 0, 200, OK); add(&m, 10, 10, OK);
    r = reader(&m);
    CHECK(part(&r, source, 10, &n) == OK && !n);
    CHECK(m.calls == 1 && !r.active && !m.idle_calls);
    CHECK(part(&r, source, 10, &n) == OK && n == 10);
    CHECK(r.started_ms == 200);

    reset(&m, source, 10); add(&m, 0, 0, NO_DATA); r = reader(&m);
    CHECK(part(&r, source, 10, &n) == NO_DATA && !n);
    CHECK(m.calls == 1 && !r.active && !m.idle_calls);

    /* Exact live regression: a 9/10-byte preamble, a soft wake, then its tail. */
    reset(&m, source, 10); add(&m, 9, 0, OK); add(&m, 0, 0, NO_DATA);
    add(&m, 0, 200, OK); add(&m, 1, 15, OK); r = reader(&m);
    CHECK(part(&r, source, 10, &n) == OK && n == 10);
    CHECK(m.calls == 4 && m.idle_calls == 2 && m.position == 10);

    reset(&m, source, 524); add(&m, 10, 0, OK); add(&m, 127, 10, OK);
    add(&m, 0, 200, NO_DATA); add(&m, 385, 20, OK);
    add(&m, 1, 5, OK); add(&m, 0, 20, NO_DATA); add(&m, 1, 6, OK);
    r = reader(&m); packet(&r, source, 512, false);
    CHECK(m.calls == 7 && m.now == 261 && m.idle_calls == 2);
}

static void test_byte_fragments_and_deadlines(const uint8_t *source) {
    mock_t m; pm3_frame_reader_t r; uint32_t n, i;
    reset(&m, source, 524);
    for (i = 0; i < 524; ++i) add(&m, 1, 1, OK);
    r = reader(&m); packet(&r, source, 512, false);
    CHECK(m.calls == 524 && m.now == 524 && r.started_ms == 0);

    /* A new part must not acquire a fresh 750-ms budget. */
    reset(&m, source, 524); add(&m, 10, 400, OK); add(&m, 512, 200, OK);
    add(&m, 2, 151, OK); r = reader(&m);
    CHECK(part(&r, source, 10, &n) == OK && n == 10);
    CHECK(part(&r, source + 10, 512, &n) == OK && n == 512);
    CHECK(part(&r, source + 522, 2, &n) == TIMEOUT && n == 2);
    CHECK(m.calls == 3 && m.now == 751);

    reset(&m, source, 10);
    for (i = 0; i < 10; ++i) add(&m, 1, i ? 100 : 0, OK);
    r = reader(&m);
    CHECK(part(&r, source, 10, &n) == TIMEOUT && n == 9);
    CHECK(m.calls == 9 && m.now == 800); /* progress never extends the budget */

    reset(&m, source, 10); add(&m, 9, 0, OK); r = reader(&m);
    CHECK(part(&r, source, 10, &n) == TIMEOUT && n == 9);
    CHECK(m.now >= 750 && m.now < 780);

    reset(&m, source, 10); r = reader(&m); r.active = true; m.now = 750;
    CHECK(part(&r, source, 10, &n) == TIMEOUT && !n && !m.calls);

    /* One completed UART call can overrun the clock budget; never accept it. */
    reset(&m, source, 10); add(&m, 10, 900, OK); r = reader(&m);
    CHECK(part(&r, source, 10, &n) == TIMEOUT && n == 10 && m.calls == 1);
}

static void test_stop_errors_and_validation(const uint8_t *source) {
    mock_t m; pm3_frame_reader_t r; uint32_t n; unsigned i;
    reset(&m, source, 10); m.run = false; r = reader(&m);
    CHECK(part(&r, source, 10, &n) == STOPPED && !n && !m.calls);

    reset(&m, source, 10); add(&m, 4, 0, OK); m.events[0].stop = true; r = reader(&m);
    CHECK(part(&r, source, 10, &n) == STOPPED && n == 4 && m.calls == 1);
    for (i = 0; i < 2; ++i) {
        int fatal = i ? IO_ERROR : DEAD;
        reset(&m, source, 10); add(&m, 0, 0, fatal); r = reader(&m);
        CHECK(part(&r, source, 10, &n) == fatal && !n && m.calls == 1);
        reset(&m, source, 10); add(&m, 9, 0, OK); add(&m, 0, 0, fatal); r = reader(&m);
        CHECK(part(&r, source, 10, &n) == fatal && n == 9 && m.calls == 2);
    }

    reset(&m, source, 10); add(&m, 10, 0, OK); m.events[0].overreport = true; r = reader(&m);
    CHECK(part(&r, source, 10, &n) == INVALID && !n && m.calls == 1);
    reset(&m, source, 10); r = reader(&m); n = 42;
    CHECK(pm3_frame_read_exact(&r, NULL, 10, &n) == INVALID && !n);
    CHECK(pm3_frame_read_exact(&r, (uint8_t *)source, 0, &n) == INVALID && !m.calls);
    r.budget_ms = 0;
    CHECK(pm3_frame_read_exact(&r, (uint8_t *)source, 10, &n) == INVALID && !m.calls);

    /* A fresh packet starts a fresh budget after an arbitrarily long idle. */
    reset(&m, source, 20); add(&m, 10, 0, OK); add(&m, 10, 20, OK); r = reader(&m);
    CHECK(part(&r, source, 10, &n) == OK && n == 10);
    m.now += 5000; r = reader(&m);
    CHECK(part(&r, source + 10, 10, &n) == OK && n == 10);
    CHECK(r.started_ms == 5000);
}

int main(void) {
    uint8_t source[1024]; unsigned i;
    for (i = 0; i < sizeof(source); ++i) source[i] = (uint8_t)(i * 73u + 19u);
    test_every_split_and_coalesced_bytes(source);
    test_idle_and_wakes(source);
    test_byte_fragments_and_deadlines(source);
    test_stop_errors_and_validation(source);
    printf("Production framed reader: %lu checks passed; no hardware used\n", checks);
    return 0;
}

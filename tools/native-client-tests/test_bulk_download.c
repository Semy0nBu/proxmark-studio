/* Execute the actual production bulk decoder/engine with deterministic I/O.
 * No serial port, RFID device, copied download algorithm or firmware writes.
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned long checks;
#define CHECK(x) do { ++checks; if (!(x)) { \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x); exit(1); \
} } while (0)

/* Deterministic allocation failures and canaries around the real staging area. */
static bool allocation_fails;
static uint8_t *allocation;
static size_t allocated_size;
static unsigned allocations, releases;
static void *test_allocate(size_t size) {
    ++allocations;
    if (allocation_fails || size > 1024u * 1024u) return NULL;
    CHECK(allocation == NULL);
    allocation = malloc(size + 32);
    CHECK(allocation != NULL);
    allocated_size = size;
    memset(allocation, 0xa5, size + 32);
    return allocation + 16;
}
static void test_release(void *pointer) {
    size_t i;
    CHECK(allocation != NULL);
    CHECK(pointer == allocation + 16);
    for (i = 0; i < 16; ++i) {
        CHECK(allocation[i] == 0xa5);
        CHECK(allocation[16 + allocated_size + i] == 0xa5);
    }
    free(allocation); allocation = NULL; ++releases;
}
#define malloc test_allocate
#define free test_release
#include "comms_bulk_download.h"
#undef malloc
#undef free

enum fault {
    NORMAL, HOLE, DUPLICATE, OVERLAP, REORDERED, OUT_OF_BOUNDS, HUGE_OFFSET,
    ZERO_LENGTH, HUGE_LENGTH, NULL_PAYLOAD, CHUNK_STATUS, DONE_STATUS,
    EARLY_DONE, SHORT_COUNT, LONG_COUNT, MISSING_DONE, INVALID_PACKET,
    CRC_DROPPED_TAIL, IGNORE_ONLY
};
typedef struct {
    pm3_bulk_packet_t packet;
    bool available;
} event_t;
typedef struct {
    uint8_t source[65536];
    event_t events[512];
    unsigned head, tail, receives, idle_calls, send_calls, sent;
    struct { uint32_t start, bytes; } requests[256];
    enum fault fault;
    unsigned fault_segment, fail_send_segment;
    uint64_t clock, receive_delay;
    bool cancelled, connected, cancel_on_receive, disconnect_on_receive;
    unsigned done_received, cancel_after_done, checks_after_done;
    bool cancel_at_final_check, disconnect_after_done;
} mock_t;

static void reset(mock_t *m) {
    unsigned i;
    memset(m, 0, sizeof(*m)); m->connected = true; m->receive_delay = 1;
    m->fault_segment = 1;
    for (i = 0; i < sizeof(m->source); ++i) m->source[i] = (uint8_t)((i * 37u) ^ (i >> 3));
    CHECK(allocation == NULL); allocation_fails = false; allocations = releases = 0;
}
static void push(mock_t *m, pm3_bulk_packet_t packet) {
    CHECK(m->tail < sizeof(m->events) / sizeof(m->events[0]));
    m->events[m->tail++] = (event_t){packet, true};
}
static void chunk(mock_t *m, uint32_t start, uint32_t offset, uint32_t length) {
    push(m, (pm3_bulk_packet_t){PM3_BULK_CHUNK, 0, offset, length, 0, m->source + start + offset});
}
static void normal_chunks(mock_t *m, uint32_t start, uint32_t length, bool drop_tail) {
    uint32_t offset = 0;
    while (offset < length) {
        uint32_t n = length - offset;
        if (n > 508) n = 508;
        if (!drop_tail || offset + n < length) chunk(m, start, offset, n);
        offset += n;
    }
}
static bool mock_send(void *context, uint32_t start, uint32_t bytes) {
    mock_t *m = context;
    enum fault fault;
    ++m->send_calls;
    if (m->send_calls == m->fail_send_segment) return false;
    CHECK(m->sent < sizeof(m->requests) / sizeof(m->requests[0]));
    CHECK(start <= sizeof(m->source) && bytes <= sizeof(m->source) - start);
    m->requests[m->sent].start = start; m->requests[m->sent].bytes = bytes; ++m->sent;
    fault = m->sent == m->fault_segment ? m->fault : NORMAL;
    switch (fault) {
        case HOLE: chunk(m, start, 1, 1); break;
        case DUPLICATE: chunk(m, start, 0, 16); chunk(m, start, 0, 16); break;
        case OVERLAP: chunk(m, start, 0, 16); chunk(m, start, 15, 16); break;
        case REORDERED: chunk(m, start, 16, 16); chunk(m, start, 0, 16); break;
        case OUT_OF_BOUNDS: chunk(m, start, bytes, 1); break;
        case HUGE_OFFSET: push(m, (pm3_bulk_packet_t){PM3_BULK_CHUNK, 0, UINT32_MAX, 1, 0, m->source}); break;
        case ZERO_LENGTH: chunk(m, start, 0, 0); break;
        case HUGE_LENGTH: push(m, (pm3_bulk_packet_t){PM3_BULK_CHUNK, 0, 0, UINT32_MAX, 0, m->source}); break;
        case NULL_PAYLOAD: push(m, (pm3_bulk_packet_t){PM3_BULK_CHUNK, 0, 0, 1, 0, NULL}); break;
        case CHUNK_STATUS: push(m, (pm3_bulk_packet_t){PM3_BULK_CHUNK, -10, 0, 1, 0, m->source}); break;
        case EARLY_DONE: chunk(m, start, 0, 16); break;
        case INVALID_PACKET: push(m, (pm3_bulk_packet_t){PM3_BULK_INVALID, 0, 0, 0, 0, NULL}); break;
        case IGNORE_ONLY: {
            unsigned i;
            for (i = 0; i < 100; ++i) push(m, (pm3_bulk_packet_t){PM3_BULK_IGNORE, 0, 0, 0, UINT32_MAX, NULL});
            return true;
        }
        case NORMAL:
        case DONE_STATUS:
        case SHORT_COUNT:
        case LONG_COUNT:
        case MISSING_DONE:
        case CRC_DROPPED_TAIL:
        default: normal_chunks(m, start, bytes, fault == CRC_DROPPED_TAIL); break;
    }
    if (fault != MISSING_DONE) {
        push(m, (pm3_bulk_packet_t){PM3_BULK_DONE, fault == DONE_STATUS ? -10 : 0,
             0, 0, bytes + (fault == LONG_COUNT ? 1u : 0u) - (fault == SHORT_COUNT ? 1u : 0u), NULL});
    }
    return true;
}
static uint64_t mock_now(void *context) { return ((mock_t *)context)->clock; }
static bool mock_cancelled(void *context) {
    mock_t *m = context;
    if (m->cancel_after_done && m->done_received >= m->cancel_after_done) {
        if (m->checks_after_done++ > 0) m->cancelled = true;
    }
    return m->cancelled;
}
static bool mock_connected(void *context) {
    mock_t *m = context;
    if (m->disconnect_after_done && m->done_received && m->checks_after_done++ > 0) m->connected = false;
    return m->connected;
}
static bool mock_receive(void *context, pm3_bulk_packet_t *packet) {
    mock_t *m = context;
    ++m->receives; m->clock += m->receive_delay;
    if (m->cancel_on_receive) m->cancelled = true;
    if (m->disconnect_on_receive) m->connected = false;
    if (m->head == m->tail) return false;
    *packet = m->events[m->head].packet;
    if (packet->type == PM3_BULK_DONE) ++m->done_received;
    return m->events[m->head++].available;
}
static void mock_idle(void *context) { ++((mock_t *)context)->idle_calls; }
static pm3_bulk_io_t interface(void) {
    pm3_bulk_io_t io = {mock_now, mock_cancelled, mock_connected, mock_send, mock_receive, mock_idle};
    return io;
}
static pm3_bulk_result_t execute(mock_t *m, uint32_t bytes, uint32_t start,
                                 uint32_t segment, uint64_t deadline) {
    uint8_t *guard = malloc((size_t)bytes + 2);
    pm3_bulk_io_t io = interface();
    pm3_bulk_result_t result;
    uint32_t i;
    CHECK(guard != NULL); memset(guard, 0xcc, (size_t)bytes + 2);
    result = pm3_bulk_download_run(guard + 1, bytes, start, segment, deadline, &io, m);
    CHECK(guard[0] == 0xcc && guard[bytes + 1] == 0xcc);
    if (result == PM3_BULK_OK) CHECK(!memcmp(guard + 1, m->source + start, bytes));
    else for (i = 1; i <= bytes; ++i) CHECK(guard[i] == 0xcc);
    CHECK(allocation == NULL); CHECK(releases == allocations || (releases == 0 && allocation_fails));
    free(guard); return result;
}

static void test_segmented_and_usb_requests(void) {
    mock_t m;
    static const uint32_t sizes[] = {1, 4, 508, 511, 512, 513, 1024, 1031, 4096, 30000};
    unsigned i, mode;
    for (mode = 0; mode < 2; ++mode) {
        for (i = 0; i < sizeof(sizes) / sizeof(sizes[0]); ++i) {
            uint32_t j, offset = 0, bytes = sizes[i];
            reset(&m);
            CHECK(execute(&m, bytes, 37, mode ? 512 : 0, 10000) == PM3_BULK_OK);
            CHECK(m.sent == (mode ? (bytes + 511) / 512 : 1));
            for (j = 0; j < m.sent; ++j) {
                CHECK(m.requests[j].start == 37 + offset);
                CHECK(m.requests[j].bytes == (mode && bytes - offset > 512 ? 512 : bytes - offset));
                offset += m.requests[j].bytes;
            }
            CHECK(offset == bytes && allocations == 1 && releases == 1);
        }
    }
    reset(&m); CHECK(execute(&m, 8, 0, 1, 1000) == PM3_BULK_OK); CHECK(m.sent == 8);
}

static void test_invalid_chunks_terminals_and_atomic_commit(void) {
    mock_t m; enum fault fault; unsigned segment;
    for (segment = 1; segment <= 2; ++segment) {
        for (fault = HOLE; fault <= CRC_DROPPED_TAIL; fault = (enum fault)(fault + 1)) {
            if (fault == MISSING_DONE) continue;
            reset(&m); m.fault = fault; m.fault_segment = segment;
            CHECK(execute(&m, 1536, 0, 512, 10000) == PM3_BULK_PROTOCOL_ERROR);
            CHECK(m.sent == segment); /* No later segment after any failed one. */
        }
    }
    reset(&m); m.fault = MISSING_DONE;
    CHECK(execute(&m, 1536, 0, 512, 20) == PM3_BULK_TIMEOUT);
    CHECK(m.sent == 1 && m.idle_calls > 0);
    /* Keeping one USB request must not retain the old early-success behavior. */
    reset(&m); m.fault = EARLY_DONE;
    CHECK(execute(&m, 1536, 0, 0, 1000) == PM3_BULK_PROTOCOL_ERROR); CHECK(m.sent == 1);
    reset(&m); m.fault = CRC_DROPPED_TAIL;
    CHECK(execute(&m, 1536, 0, 0, 1000) == PM3_BULK_PROTOCOL_ERROR); CHECK(m.sent == 1);
}

static void test_one_deadline_without_progress_or_wtx_extensions(void) {
    mock_t m;
    reset(&m); m.receive_delay = 4;
    CHECK(execute(&m, 1536, 0, 512, 20) == PM3_BULK_TIMEOUT);
    CHECK(m.sent == 2 && m.clock == 20); /* Three segment-local budgets would pass. */
    reset(&m); m.receive_delay = 3;
    CHECK(execute(&m, 512, 0, 512, 9) == PM3_BULK_TIMEOUT); CHECK(m.done_received == 1);
    reset(&m); m.receive_delay = 3;
    CHECK(execute(&m, 512, 0, 512, 10) == PM3_BULK_OK); CHECK(m.done_received == 1);
    reset(&m); m.fault = IGNORE_ONLY; m.receive_delay = 5;
    CHECK(execute(&m, 1024, 0, 512, 25) == PM3_BULK_TIMEOUT);
    CHECK(m.sent == 1 && m.receives == 5);
    reset(&m); m.receive_delay = 100000;
    CHECK(execute(&m, 1024, 0, 512, UINT64_MAX) == PM3_BULK_OK);
    reset(&m); CHECK(execute(&m, 512, 0, 512, 0) == PM3_BULK_TIMEOUT);
    CHECK(m.sent == 0 && allocations == 0);
}

static void test_cancellation_disconnect_send_and_allocation_failure(void) {
    mock_t m;
    reset(&m); m.cancelled = true;
    CHECK(execute(&m, 1024, 0, 512, 1000) == PM3_BULK_CANCELLED); CHECK(!m.sent && !allocations);
    reset(&m); m.connected = false;
    CHECK(execute(&m, 1024, 0, 512, 1000) == PM3_BULK_DISCONNECTED); CHECK(!m.sent && !allocations);
    reset(&m); m.cancel_on_receive = true;
    CHECK(execute(&m, 1024, 0, 512, 1000) == PM3_BULK_CANCELLED); CHECK(m.sent == 1);
    reset(&m); m.disconnect_on_receive = true;
    CHECK(execute(&m, 1024, 0, 512, 1000) == PM3_BULK_DISCONNECTED); CHECK(m.sent == 1);
    reset(&m); m.cancel_after_done = 1;
    CHECK(execute(&m, 1024, 0, 512, 1000) == PM3_BULK_CANCELLED); CHECK(m.sent == 1);
    reset(&m); m.cancel_after_done = 1;
    CHECK(execute(&m, 512, 0, 512, 1000) == PM3_BULK_CANCELLED); CHECK(m.sent == 1);
    reset(&m); m.disconnect_after_done = true;
    CHECK(execute(&m, 512, 0, 512, 1000) == PM3_BULK_DISCONNECTED); CHECK(m.sent == 1);
    reset(&m); m.fail_send_segment = 1;
    CHECK(execute(&m, 1024, 0, 512, 1000) == PM3_BULK_SEND_FAILED); CHECK(m.sent == 0 && m.send_calls == 1);
    reset(&m); m.fail_send_segment = 2;
    CHECK(execute(&m, 1536, 0, 512, 1000) == PM3_BULK_SEND_FAILED); CHECK(m.sent == 1 && m.send_calls == 2);
    reset(&m); allocation_fails = true;
    CHECK(execute(&m, 1024, 0, 512, 1000) == PM3_BULK_ALLOCATION_FAILED); CHECK(!m.sent && !releases);
}

static void test_bad_arguments_do_not_send(void) {
    mock_t m; pm3_bulk_io_t io = interface(), bad; uint8_t out[8];
    reset(&m);
    CHECK(pm3_bulk_download_run(NULL, 1, 0, 512, 100, &io, &m) == PM3_BULK_PROTOCOL_ERROR);
    CHECK(pm3_bulk_download_run(out, 1, 0, 512, 100, NULL, &m) == PM3_BULK_PROTOCOL_ERROR);
    CHECK(pm3_bulk_download_run(out, 4, UINT32_MAX - 3, 512, 100, &io, &m) == PM3_BULK_PROTOCOL_ERROR);
    CHECK(pm3_bulk_download_run(out, 0, UINT32_MAX, 512, 100, &io, &m) == PM3_BULK_OK);
    bad = io; bad.now = NULL;
    CHECK(pm3_bulk_download_run(out, 1, 0, 512, 100, &bad, &m) == PM3_BULK_PROTOCOL_ERROR);
    bad = io; bad.receive = NULL;
    CHECK(pm3_bulk_download_run(out, 1, 0, 512, 100, &bad, &m) == PM3_BULK_PROTOCOL_ERROR);
    bad = io; bad.send = NULL;
    CHECK(pm3_bulk_download_run(out, 1, 0, 512, 100, &bad, &m) == PM3_BULK_PROTOCOL_ERROR);
    CHECK(!m.sent && !allocations);
}

static void test_actual_ng_wire_decoder(void) {
    pm3_bulk_packet_t packet;
    uint8_t wire[16] = {0};
    uint32_t offset = 0x12345678u, sent = 512;
    unsigned i;
    memcpy(wire + 1, &offset, sizeof(offset)); wire[5] = 0xac; wire[6] = 0xfe;
    pm3_bulk_decode(&packet, true, 20, 0, wire + 1, 6, 20, 21, 99);
    CHECK(packet.type == PM3_BULK_CHUNK && packet.offset == offset && packet.length == 2);
    CHECK(packet.data[0] == 0xac && packet.data[1] == 0xfe);
    for (i = 0; i < 4; ++i) {
        pm3_bulk_decode(&packet, true, 20, 0, wire, i, 20, 21, 99);
        CHECK(packet.type == PM3_BULK_INVALID);
    }
    pm3_bulk_decode(&packet, false, 20, 0, wire, 6, 20, 21, 99); CHECK(packet.type == PM3_BULK_INVALID);
    pm3_bulk_decode(&packet, true, 20, 0, NULL, 6, 20, 21, 99); CHECK(packet.type == PM3_BULK_INVALID);
    memcpy(wire + 1, &sent, sizeof(sent));
    for (i = 0; i <= 12; ++i) {
        pm3_bulk_decode(&packet, true, 21, 0, wire + 1, i, 20, 21, 99);
        CHECK(packet.type == (i == 8 ? PM3_BULK_DONE : PM3_BULK_INVALID));
        if (i == 8) CHECK(packet.bytes_sent == sent);
    }
    pm3_bulk_decode(&packet, false, 21, 0, wire, 8, 20, 21, 99); CHECK(packet.type == PM3_BULK_INVALID);
    pm3_bulk_decode(&packet, true, 21, 0, NULL, 8, 20, 21, 99); CHECK(packet.type == PM3_BULK_INVALID);
    pm3_bulk_decode(&packet, true, 21, -10, wire, 8, 20, 21, 99); CHECK(packet.type == PM3_BULK_DONE && packet.status == -10);
    pm3_bulk_decode(&packet, false, 99, 0, wire, 8, 20, 21, 99); CHECK(packet.type == PM3_BULK_INVALID);
    pm3_bulk_decode(&packet, true, 99, 0, wire, 8, 20, 21, 99); CHECK(packet.type == PM3_BULK_INVALID);
    pm3_bulk_decode(&packet, true, 77, 0, NULL, UINT32_MAX, 20, 21, 99); CHECK(packet.type == PM3_BULK_IGNORE);
}

int main(void) {
    test_actual_ng_wire_decoder();
    test_segmented_and_usb_requests();
    test_invalid_chunks_terminals_and_atomic_commit();
    test_one_deadline_without_progress_or_wtx_extensions();
    test_cancellation_disconnect_send_and_allocation_failure();
    test_bad_arguments_do_not_send();
    printf("Production bulk decoder/engine: %lu checks passed; hardware used: no\n", checks);
    return 0;
}

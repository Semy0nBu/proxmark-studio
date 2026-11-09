/* Execute the production Win32 I/O helpers with API mocks, never serial handles.
 * API events describe driver results; all read/write/configuration decisions
 * come from uart_win32_io.h and framed assembly from frame_reader.h.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned checks, scenarios;
#define CHECK(value) do { ++checks; if (!(value)) { \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #value); exit(1); \
} } while (0)

#define PORT ((HANDLE)(uintptr_t)0x100)
#define EVENT ((HANDLE)(uintptr_t)0x200)

typedef struct {
    DWORD queued, returned, error, wait_result;
    BOOL clear_ok, immediate_ok, completion_ok;
    DWORD completion_error, completion_count;
} io_event_t;

typedef struct {
    io_event_t events[64], current;
    unsigned event_count, event_index;
    const uint8_t *source;
    unsigned source_size, position;
    DWORD error, last_request, wait_ms, configured_baud;
    unsigned read_calls, write_calls, clear_calls, cancel_calls, completion_calls;
    unsigned timeout_calls, speed_calls, reset_calls, wait_calls;
    BOOL timeout_ok, speed_ok, cancel_ok, reset_ok, pending, writing, incomplete_once;
    BOOL change_timeout_before_next_read;
    DWORD timeout_error, speed_error, cancel_error;
    COMMTIMEOUTS configured;
    COMMTIMEOUTS active;
    OVERLAPPED *operation;
    void *buffer;
    const void *write_buffer;
    uint64_t now;
} mock_t;
static mock_t mock;

static void reset(void) {
    memset(&mock, 0, sizeof(mock));
    mock.timeout_ok = mock.speed_ok = mock.cancel_ok = mock.reset_ok = TRUE;
    mock.configured_baud = 115200;
    mock.wait_ms = 100;
}

static io_event_t ordinary(DWORD queued, DWORD returned) {
    io_event_t value;
    memset(&value, 0, sizeof(value));
    value.queued = queued;
    value.returned = returned;
    value.clear_ok = value.immediate_ok = value.completion_ok = TRUE;
    value.wait_result = WAIT_OBJECT_0;
    value.completion_count = returned;
    return value;
}

static void add(io_event_t value) {
    CHECK(mock.event_count < sizeof(mock.events) / sizeof(mock.events[0]));
    mock.events[mock.event_count++] = value;
}

static void transfer(void *destination, DWORD count) {
    CHECK(mock.source != NULL);
    CHECK(mock.position + count <= mock.source_size);
    memcpy(destination, mock.source + mock.position, count);
    mock.position += count;
}

static BOOL WINAPI fake_ClearCommError(HANDLE port, LPDWORD errors, LPCOMSTAT state) {
    CHECK(port == PORT && errors && state && !mock.pending);
    ++mock.clear_calls;
    CHECK(mock.event_index < mock.event_count);
    mock.current = mock.events[mock.event_index++];
    memset(state, 0, sizeof(*state));
    state->cbInQue = mock.current.queued;
    *errors = 0;
    mock.error = mock.current.error;
    mock.now += 10;
    return mock.current.clear_ok;
}

static BOOL WINAPI fake_ResetEvent(HANDLE event) {
    CHECK(event == EVENT && !mock.pending);
    ++mock.reset_calls;
    if (!mock.reset_ok) mock.error = ERROR_INVALID_HANDLE;
    return mock.reset_ok;
}

static BOOL WINAPI fake_ReadFile(HANDLE port, LPVOID buffer, DWORD maximum,
                               LPDWORD count, LPOVERLAPPED operation) {
    CHECK(port == PORT && buffer && count && operation);
    CHECK(operation->hEvent == EVENT && !mock.pending);
    CHECK(maximum > 0 && maximum <= mock.current.queued);
    CHECK(mock.current.returned <= maximum);
    ++mock.read_calls;
    mock.last_request = maximum;
    mock.operation = operation;
    mock.buffer = buffer;
    mock.writing = FALSE;
    *count = 0;
    if (mock.current.immediate_ok) {
        *count = mock.current.returned;
        transfer(buffer, *count);
        return TRUE;
    }
    mock.error = mock.current.error;
    mock.pending = mock.error == ERROR_IO_PENDING;
    return FALSE;
}

static BOOL WINAPI fake_WriteFile(HANDLE port, LPCVOID buffer, DWORD length,
                                LPDWORD count, LPOVERLAPPED operation) {
    CHECK(port == PORT && buffer && count && operation);
    CHECK(operation->hEvent == EVENT && !mock.pending);
    CHECK(mock.event_index < mock.event_count);
    mock.current = mock.events[mock.event_index++];
    CHECK(mock.current.returned <= length);
    ++mock.write_calls;
    mock.last_request = length;
    mock.operation = operation;
    mock.write_buffer = buffer;
    mock.writing = TRUE;
    *count = mock.current.immediate_ok ? mock.current.returned : 0;
    mock.error = mock.current.error;
    mock.pending = !mock.current.immediate_ok && mock.error == ERROR_IO_PENDING;
    return mock.current.immediate_ok;
}

static DWORD WINAPI fake_WaitForSingleObject(HANDLE event, DWORD wait_ms) {
    CHECK(event == EVENT && mock.pending);
    CHECK(wait_ms == mock.wait_ms);
    ++mock.wait_calls;
    return mock.current.wait_result;
}

static BOOL WINAPI fake_CancelIoEx(HANDLE port, LPOVERLAPPED operation) {
    CHECK(port == PORT && mock.pending && operation == mock.operation);
    ++mock.cancel_calls;
    mock.error = mock.cancel_error;
    return mock.cancel_ok;  /* Cancellation is asynchronous: keep it pending. */
}

static BOOL WINAPI fake_GetOverlappedResult(HANDLE port, LPOVERLAPPED operation,
                                          LPDWORD count, BOOL wait) {
    CHECK(port == PORT && operation == mock.operation && mock.pending);
    CHECK(operation->hEvent == EVENT && count);
    if (mock.cancel_calls || mock.current.wait_result != WAIT_OBJECT_0) CHECK(wait == TRUE);
    ++mock.completion_calls;
    if (mock.incomplete_once && mock.completion_calls == 1) {
        mock.error = ERROR_IO_INCOMPLETE;
        return FALSE;  /* Kernel operation is still pending until retired. */
    }
    mock.pending = FALSE;
    *count = mock.current.completion_ok ? mock.current.completion_count : 0;
    if (mock.current.completion_ok && !mock.writing) transfer(mock.buffer, *count);
    mock.error = mock.current.completion_error;
    return mock.current.completion_ok;
}

static DWORD WINAPI fake_GetLastError(void) { return mock.error; }

static BOOL WINAPI fake_SetCommTimeouts(HANDLE port, LPCOMMTIMEOUTS timeouts) {
    CHECK(port == PORT && timeouts && !mock.pending);
    ++mock.timeout_calls;
    mock.configured = *timeouts;
    mock.error = mock.timeout_error;
    return mock.timeout_ok;
}

static BOOL WINAPI fake_GetCommState(HANDLE port, LPDCB state) {
    CHECK(port == PORT && state);
    ++mock.speed_calls;
    if (mock.speed_ok) state->BaudRate = mock.configured_baud;
    mock.error = mock.speed_error;
    return mock.speed_ok;
}

static BOOL WINAPI forbidden_PurgeComm(HANDLE port, DWORD flags) {
    (void)port; (void)flags; CHECK(false); return FALSE;
}
static BOOL WINAPI forbidden_CloseHandle(HANDLE handle) {
    (void)handle; CHECK(false); return FALSE;
}
static void forbidden_free(void *pointer) { (void)pointer; CHECK(false); }

#define ClearCommError fake_ClearCommError
#define ResetEvent fake_ResetEvent
#define ReadFile fake_ReadFile
#define WriteFile fake_WriteFile
#define WaitForSingleObject fake_WaitForSingleObject
#define CancelIoEx fake_CancelIoEx
#define GetOverlappedResult fake_GetOverlappedResult
#define GetLastError fake_GetLastError
#define SetCommTimeouts fake_SetCommTimeouts
#define GetCommState fake_GetCommState
#define PurgeComm forbidden_PurgeComm
#define CloseHandle forbidden_CloseHandle
#define free forbidden_free
#include "uart_win32_io.h"
#include "frame_reader.h"
#undef free

static int frame_receive(void *context, uint8_t *output, uint32_t maximum, uint32_t *count) {
    DWORD received = 0;
    DWORD error;
    (void)context;
    if (mock.change_timeout_before_next_read && mock.position == 20) {
        CHECK(uart_win32_configure_timeouts(PORT, &mock.active, 20) == ERROR_SUCCESS);
        mock.change_timeout_before_next_read = FALSE;
    }
    error = uart_win32_read_some(PORT, EVENT, output, maximum, &received, mock.wait_ms);
    *count = received;
    return error == ERROR_SUCCESS ? 0 : error == ERROR_NO_DATA ? 1 : -1;
}
static uint64_t frame_now(void *context) { (void)context; return mock.now; }
static bool frame_running(void *context) { (void)context; return true; }
static void frame_idle(void *context) { (void)context; ++mock.now; }

static void test_short_reads_and_empty_intervals(void) {
    uint8_t source[32], output[34];
    unsigned i, mode;
    for (i = 0; i < sizeof(source); ++i) source[i] = (uint8_t)(17u + i * 7u);
    for (mode = 0; mode < 4; ++mode) {
        pm3_frame_reader_t frame;
        uint32_t count = 0;
        reset();mock.source = source;mock.source_size = sizeof(source);
        if (mode == 3) {
            mock.change_timeout_before_next_read = TRUE;
            mock.active.ReadIntervalTimeout = mock.active.ReadTotalTimeoutConstant = 200;
        }
        add(ordinary(mode == 0 ? 32 : 20, 20));
        if (mode == 2) { add(ordinary(0, 0)); add(ordinary(0, 0)); }
        add(ordinary(12, 12));
        memset(output, 0xcc, sizeof(output));
        frame = (pm3_frame_reader_t){.context = NULL, .receive = frame_receive,
            .now = frame_now, .running = frame_running, .idle = frame_idle,
            .success = 0, .no_data = 1, .timeout = -2, .stopped = -3,
            .invalid = -4, .budget_ms = PM3_FRAME_RX_BUDGET_MS};
        CHECK(pm3_frame_read_exact(&frame, output + 1, 32, &count) == 0);
        CHECK(count == 32 && !memcmp(source, output + 1, 32));
        CHECK(output[0] == 0xcc && output[33] == 0xcc);
        CHECK(mock.read_calls == 2 && mock.position == 32 && !mock.pending);
        CHECK(mock.event_index == mock.event_count && mock.last_request == 12);
        if (mode == 3) CHECK(mock.active.ReadTotalTimeoutConstant == 20 && mock.timeout_calls == 1);
        ++scenarios;
    }
}

static void test_speed_and_runtime_configuration(void) {
    COMMTIMEOUTS active, before;
    DCB state;
    reset();memset(&state, 0, sizeof(state));state.DCBlength = sizeof(state);
    CHECK(uart_win32_query_speed(PORT, &state) == 115200);
    CHECK(state.BaudRate == 115200 && mock.speed_calls == 1);
    mock.speed_ok = FALSE;mock.speed_error = ERROR_INVALID_HANDLE;
    CHECK(uart_win32_query_speed(PORT, &state) == 0 && mock.speed_calls == 2);
    memset(&active, 0, sizeof(active));active.ReadIntervalTimeout = 200;
    active.ReadTotalTimeoutConstant = 200;
    CHECK(uart_win32_configure_timeouts(PORT, &active, 20) == ERROR_SUCCESS);
    CHECK(active.ReadIntervalTimeout == 20 && active.ReadTotalTimeoutConstant == 20);
    CHECK(!memcmp(&active, &mock.configured, sizeof(active)));
    before = active;mock.timeout_ok = FALSE;mock.timeout_error = ERROR_INVALID_HANDLE;
    CHECK(uart_win32_configure_timeouts(PORT, &active, 200) == ERROR_INVALID_HANDLE);
    CHECK(!memcmp(&active, &before, sizeof(active)));
    CHECK(!mock.read_calls && !mock.write_calls && !mock.pending);
    scenarios += 4;
}

static void test_pending_reads_and_retirement(void) {
    uint8_t source[32], output[32];
    DWORD count, error;
    unsigned i;
    memset(source, 0x5a, sizeof(source));
    for (i = 0; i < 5; ++i) {
        io_event_t event = ordinary(32, 0);
        reset();mock.source = source;mock.source_size = sizeof(source);
        event.immediate_ok = FALSE;event.error = ERROR_IO_PENDING;
        event.completion_count = 20;
        if (i > 0) event.wait_result = i == 4 ? WAIT_FAILED : WAIT_TIMEOUT;
        if (i == 1 || i == 4) {
            event.completion_ok = FALSE;event.completion_error = ERROR_OPERATION_ABORTED;
        }
        if (i == 3) { mock.cancel_ok = FALSE;mock.cancel_error = ERROR_NOT_FOUND; }
        add(event);count = 123;
        error = uart_win32_read_some(PORT, EVENT, output, 32, &count, 100);
        CHECK(!mock.pending && mock.read_calls == 1 && mock.completion_calls == 1);
        CHECK(mock.cancel_calls == (i == 0 ? 0u : 1u));
        if (i == 0) CHECK(error == ERROR_SUCCESS && count == 20 && !memcmp(source, output, 20));
        else CHECK(error != ERROR_SUCCESS);
        ++scenarios;
    }
}

static void test_writes_require_complete_success_without_replay(void) {
    uint8_t source[32];
    unsigned i;
    memset(source, 0x6b, sizeof(source));
    for (i = 0; i < 6; ++i) {
        io_event_t event = ordinary(0, i == 1 ? 20 : 32);
        DWORD count = 123, error;
        reset();
        if (i >= 2) {
            event.immediate_ok = FALSE;event.error = ERROR_IO_PENDING;
            event.completion_count = i == 3 ? 20 : 32;
        }
        if (i >= 4) event.wait_result = WAIT_TIMEOUT;
        if (i == 4) { event.completion_ok = FALSE;event.completion_error = ERROR_OPERATION_ABORTED; }
        add(event);
        error = uart_win32_write_exact(PORT, EVENT, source, 32, &count, 100);
        CHECK(mock.write_calls == 1 && !mock.pending && mock.write_buffer == source);
        CHECK(mock.last_request == 32);
        if (i == 0 || i == 2) CHECK(error == ERROR_SUCCESS && count == 32);
        else if (i == 1 || i == 3) CHECK(error == ERROR_WRITE_FAULT && count == 20);
        else CHECK(error == ERROR_TIMEOUT);
        CHECK(mock.cancel_calls == (i >= 4 ? 1u : 0u));
        ++scenarios;
    }
}

static void test_driver_errors_and_pending_incomplete_retire_before_return(void) {
    uint8_t source[32], output[32];
    DWORD count, error;
    unsigned mode;
    memset(source, 0x7c, sizeof(source));
    for (mode = 0; mode < 4; ++mode) {
        io_event_t event = ordinary(32, 0);
        reset();mock.source = source;mock.source_size = sizeof(source);
        event.error = ERROR_DEVICE_NOT_CONNECTED;
        if (mode == 0) event.clear_ok = FALSE;
        if (mode == 1) event.immediate_ok = FALSE;
        if (mode == 2) mock.reset_ok = FALSE;
        if (mode == 3) {
            event.immediate_ok = FALSE;event.error = ERROR_IO_PENDING;
            event.completion_ok = FALSE;event.completion_error = ERROR_DEVICE_NOT_CONNECTED;
        }
        add(event);count = 123;
        error = uart_win32_read_some(PORT, EVENT, output, 32, &count, 100);
        CHECK(error != ERROR_SUCCESS && count == 0 && !mock.pending);
        CHECK(mock.read_calls == (mode == 1 || mode == 3 ? 1u : 0u));
        CHECK(!mock.cancel_calls);
        ++scenarios;
    }
    {
        io_event_t event = ordinary(32, 0);
        reset();mock.source = source;mock.source_size = sizeof(source);
        mock.incomplete_once = TRUE;
        event.immediate_ok = FALSE;event.error = ERROR_IO_PENDING;
        event.completion_count = 20;
        add(event);
        error = uart_win32_read_some(PORT, EVENT, output, 32, &count, 100);
        CHECK(error == ERROR_IO_INCOMPLETE && !mock.pending);
        CHECK(mock.completion_calls == 2 && mock.cancel_calls == 1 && mock.read_calls == 1);
        ++scenarios;
    }
    for (mode = 0; mode < 3; ++mode) {
        io_event_t event = ordinary(0, 0);
        reset();event.immediate_ok = FALSE;event.error = ERROR_DEVICE_NOT_CONNECTED;
        if (mode == 1) mock.reset_ok = FALSE;
        if (mode == 2) {
            event.error = ERROR_IO_PENDING;event.completion_ok = FALSE;
            event.completion_error = ERROR_DEVICE_NOT_CONNECTED;
        }
        add(event);
        error = uart_win32_write_exact(PORT, EVENT, source, 32, &count, 100);
        CHECK(error != ERROR_SUCCESS && !mock.pending);
        CHECK(mock.write_calls == (mode == 1 ? 0u : 1u));
        CHECK(!mock.cancel_calls);
        ++scenarios;
    }
}

static void test_budget_and_invalid_arguments(void) {
    uint8_t byte = 0;
    DWORD count = 123;
    reset();
    CHECK(uart_win32_io_budget(0, 0, 32) == 250);
    CHECK(uart_win32_io_budget(200, 0, 32) == 6650);
    CHECK(uart_win32_io_budget(0, 20, 32) == 270);
    CHECK(uart_win32_io_budget(MAXDWORD, MAXDWORD, MAXDWORD) == INFINITE - 1);
    CHECK(uart_win32_configure_timeouts(PORT, NULL, 20) == ERROR_INVALID_PARAMETER);
    CHECK(uart_win32_query_speed(PORT, NULL) == 0);
    CHECK(uart_win32_read_some(PORT, EVENT, &byte, 1, NULL, 100) == ERROR_INVALID_PARAMETER);
    CHECK(uart_win32_read_some(PORT, EVENT, NULL, 1, &count, 100) == ERROR_INVALID_PARAMETER && count == 0);
    CHECK(uart_win32_write_exact(PORT, EVENT, NULL, 1, &count, 100) == ERROR_INVALID_PARAMETER && count == 0);
    CHECK(!mock.clear_calls && !mock.read_calls && !mock.write_calls && !mock.pending);
    ++scenarios;
}

int main(void) {
    /* Keep forbidden mock symbols referenced even if the correct header never
     * calls them; any future purge/close/free in these helpers fails at runtime. */
    (void)forbidden_PurgeComm;(void)forbidden_CloseHandle;(void)forbidden_free;
    test_short_reads_and_empty_intervals();
    test_speed_and_runtime_configuration();
    test_pending_reads_and_retirement();
    test_writes_require_complete_success_without_replay();
    test_driver_errors_and_pending_incomplete_retire_before_return();
    test_budget_and_invalid_arguments();
    printf("Production Win32 serial I/O: %u scenarios, %u checks passed; no device I/O\n", scenarios, checks);
    return 0;
}

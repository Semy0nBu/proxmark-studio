/* Tests the actual production helper, with Win32 completion APIs substituted.
 * No serial handle, Bluetooth device or other hardware is opened.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned assertions, cases;
#define CHECK(expression) do { \
    ++assertions; \
    if (!(expression)) { \
        fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #expression); \
        exit(1); \
    } \
} while (0)

typedef struct {
    HANDLE port, wake;
    OVERLAPPED *operation;
    DWORD *event_mask;
    BOOL pending, cancel_ok, result_ok;
    DWORD cancel_error, result_error, last_error;
    char calls[12];
    unsigned used;
} Mock;
static Mock mock;

static void record(char call) {
    CHECK(mock.used + 1 < sizeof(mock.calls));
    mock.calls[mock.used++] = call;
    mock.calls[mock.used] = 0;
}
static BOOL WINAPI mock_ResetEvent(HANDLE event) {
    CHECK(event == mock.wake);
    CHECK(mock.pending);
    record('R');
    return TRUE;
}
static BOOL WINAPI mock_CancelIoEx(HANDLE port, LPOVERLAPPED operation) {
    CHECK(port == mock.port);
    CHECK(operation == mock.operation);
    CHECK(mock.pending);
    record('C');
    /* Deliberately leave completion pending: cancellation is only a request. */
    mock.last_error = mock.cancel_error;
    return mock.cancel_ok;
}
static BOOL WINAPI mock_GetOverlappedResult(
    HANDLE port, LPOVERLAPPED operation, LPDWORD transferred, BOOL wait) {
    CHECK(port == mock.port);
    CHECK(operation == mock.operation);
    CHECK(mock.pending);
    CHECK(wait == TRUE);
    CHECK(operation->hEvent == (HANDLE)(uintptr_t)0x300);
    record('G');
    /* Simulate the kernel's final write while caller-owned stack storage lives. */
    *mock.event_mask = mock.result_ok ? EV_RXCHAR : 0;
    *transferred = 0;
    operation->Internal = mock.result_ok ? 0 : mock.result_error;
    mock.pending = FALSE;
    mock.last_error = mock.result_error;
    return mock.result_ok;
}
static DWORD WINAPI mock_GetLastError(void) {
    record('E');
    return mock.last_error;
}

#define ResetEvent mock_ResetEvent
#define CancelIoEx mock_CancelIoEx
#define GetOverlappedResult mock_GetOverlappedResult
#define GetLastError mock_GetLastError
#include "uart_win32_wait.h"
#undef GetLastError
#undef GetOverlappedResult
#undef CancelIoEx
#undef ResetEvent

static void scenario(DWORD wait_result, BOOL cancel_ok, DWORD cancel_error,
                     BOOL result_ok, DWORD result_error,
                     uart_comm_wait_result_t expected, const char *calls) {
    OVERLAPPED operation;
    DWORD event_mask = 0xdeadbeef;
    memset(&operation, 0, sizeof(operation));
    operation.hEvent = (HANDLE)(uintptr_t)0x300;
    memset(&mock, 0, sizeof(mock));
    mock.port = (HANDLE)(uintptr_t)0x100;
    mock.wake = (HANDLE)(uintptr_t)0x200;
    mock.operation = &operation;
    mock.event_mask = &event_mask;
    mock.pending = TRUE;
    mock.cancel_ok = cancel_ok;
    mock.cancel_error = cancel_error;
    mock.result_ok = result_ok;
    mock.result_error = result_error;
    CHECK(uart_complete_comm_wait(mock.port, mock.wake, &operation, wait_result) == expected);
    CHECK(mock.pending == FALSE);
    CHECK(event_mask == (result_ok ? EV_RXCHAR : 0));
    CHECK(strcmp(mock.calls, calls) == 0);
    ++cases;
}

int main(void) {
    /* Ordinary completion and failed completions must also be consumed. */
    scenario(WAIT_OBJECT_0, TRUE, 0, TRUE, 0, UART_COMM_WAIT_READY, "G");
    scenario(WAIT_OBJECT_0, TRUE, 0, FALSE, ERROR_OPERATION_ABORTED, UART_COMM_WAIT_WAKE, "GE");
    scenario(WAIT_OBJECT_0, TRUE, 0, FALSE, ERROR_DEVICE_NOT_CONNECTED, UART_COMM_WAIT_ERROR, "GE");

    /* All early-return paths retire the OVERLAPPED after cancellation. */
    scenario(WAIT_TIMEOUT, TRUE, 0, FALSE, ERROR_OPERATION_ABORTED, UART_COMM_WAIT_EMPTY, "CGE");
    scenario(WAIT_OBJECT_0 + 1, TRUE, 0, FALSE, ERROR_OPERATION_ABORTED, UART_COMM_WAIT_WAKE, "RCGE");
    scenario(WAIT_FAILED, TRUE, 0, FALSE, ERROR_OPERATION_ABORTED, UART_COMM_WAIT_ERROR, "CGE");
    scenario(WAIT_ABANDONED_0, TRUE, 0, FALSE, ERROR_OPERATION_ABORTED, UART_COMM_WAIT_ERROR, "CGE");

    /* Completion can win the cancel race; ERROR_NOT_FOUND is not an early exit. */
    scenario(WAIT_TIMEOUT, FALSE, ERROR_NOT_FOUND, TRUE, 0, UART_COMM_WAIT_EMPTY, "CG");
    scenario(WAIT_OBJECT_0 + 1, FALSE, ERROR_NOT_FOUND, TRUE, 0, UART_COMM_WAIT_WAKE, "RCG");
    scenario(WAIT_FAILED, FALSE, ERROR_NOT_FOUND, TRUE, 0, UART_COMM_WAIT_ERROR, "CG");
    scenario(WAIT_TIMEOUT, FALSE, ERROR_NOT_FOUND, FALSE, ERROR_DEVICE_NOT_CONNECTED, UART_COMM_WAIT_ERROR, "CGE");

    /* A successful cancel request may still complete normally. */
    scenario(WAIT_TIMEOUT, TRUE, 0, TRUE, 0, UART_COMM_WAIT_EMPTY, "CG");
    scenario(WAIT_OBJECT_0 + 1, TRUE, 0, TRUE, 0, UART_COMM_WAIT_WAKE, "RCG");

    /* Never leave the operation pending because CancelIoEx itself failed. */
    scenario(WAIT_TIMEOUT, FALSE, ERROR_ACCESS_DENIED, FALSE, ERROR_OPERATION_ABORTED, UART_COMM_WAIT_EMPTY, "CGE");
    scenario(WAIT_OBJECT_0 + 1, FALSE, ERROR_ACCESS_DENIED, FALSE, ERROR_INVALID_HANDLE, UART_COMM_WAIT_ERROR, "RCGE");

    printf("UART WaitCommEvent: %u scenarios, %u checks passed; no device I/O\n", cases, assertions);
    return 0;
}

//-----------------------------------------------------------------------------
// Copyright (C) Proxmark3 contributors. See AUTHORS.md for details.
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// See LICENSE.txt for the text of the license.
//-----------------------------------------------------------------------------

// Execute the production DCB helpers and extracted uart_open/uart_set_speed code.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>

static unsigned checks, scenarios;
#define CHECK(value) do { ++checks; if (!(value)) { \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #value); exit(1); \
} } while (0)

#define PORT ((HANDLE)(uintptr_t)0x100)
#define WARNING 1
#define INVALID_SERIAL_PORT ((void *)(intptr_t)-1)

typedef struct {
    DCB driver, submitted[8];
    DWORD error, get_error, build_error, set_errors[8];
    BOOL get_ok, build_ok, set_ok[8];
    unsigned gets, builds, sets, closes, logs;
    char order[32], message[512];
} mock_t;
static mock_t mock;

static void event(char value) {
    size_t n = strlen(mock.order);
    CHECK(n + 1 < sizeof(mock.order));
    mock.order[n] = value;
}

static void reset(void) {
    ++scenarios;
    memset(&mock, 0, sizeof(mock));
    mock.get_ok = mock.build_ok = TRUE;
    for (unsigned i = 0; i < 8; ++i) mock.set_ok[i] = TRUE;
    mock.driver.DCBlength = sizeof(DCB);
    mock.driver.BaudRate = 9600;
    mock.driver.ByteSize = 7;
    mock.driver.Parity = EVENPARITY;
    mock.driver.StopBits = TWOSTOPBITS;
    mock.driver.fBinary = FALSE;
    mock.driver.fParity = TRUE;
    mock.driver.fOutxCtsFlow = TRUE;
    mock.driver.fOutxDsrFlow = TRUE;
    mock.driver.fDtrControl = DTR_CONTROL_HANDSHAKE;
    mock.driver.fDsrSensitivity = TRUE;
    mock.driver.fTXContinueOnXoff = FALSE;
    mock.driver.fOutX = TRUE;
    mock.driver.fInX = TRUE;
    mock.driver.fErrorChar = TRUE;
    mock.driver.fNull = TRUE;
    mock.driver.fRtsControl = RTS_CONTROL_HANDSHAKE;
    mock.driver.fAbortOnError = TRUE;
    mock.driver.fDummy2 = 0x1ffff;
    mock.driver.XonChar = '#';
    mock.driver.XoffChar = '$';
    mock.driver.XonLim = 40;
    mock.driver.XoffLim = 50;
    mock.driver.EofChar = '\x1a';
    mock.driver.ErrorChar = '?';
    mock.driver.EvtChar = '!';
}

static DWORD WINAPI fake_GetLastError(void) {
    return mock.error;
}

static void WINAPI fake_SetLastError(DWORD value) {
    mock.error = value;
}

static BOOL WINAPI fake_GetCommState(HANDLE port, LPDCB dcb) {
    CHECK(port == PORT && dcb && dcb->DCBlength == sizeof(DCB));
    event('G');
    ++mock.gets;
    if (!mock.get_ok) {
        mock.error = mock.get_error;
        return FALSE;
    }
    *dcb = mock.driver;
    return TRUE;
}

static BOOL WINAPI fake_BuildCommDCBA(LPCSTR text, LPDCB dcb) {
    CHECK(dcb && dcb->DCBlength == sizeof(DCB));
    CHECK(strcmp(text, "baud=115200 parity=N data=8 stop=1") == 0);
    event('B');
    ++mock.builds;
    if (!mock.build_ok) {
        mock.error = mock.build_error;
        return FALSE;
    }
    // Like the API, leave unspecified flow-control fields and characters alone.
    dcb->BaudRate = 115200;
    dcb->ByteSize = 8;
    dcb->Parity = NOPARITY;
    dcb->StopBits = ONESTOPBIT;
    return TRUE;
}

static BOOL WINAPI fake_SetCommState(HANDLE port, LPDCB dcb) {
    CHECK(port == PORT && dcb && mock.sets < 8);
    event('S');
    unsigned index = mock.sets++;
    mock.submitted[index] = *dcb;
    if (!mock.set_ok[index]) {
        mock.error = mock.set_errors[index];
        return FALSE;
    }
    return TRUE;
}

#define GetLastError fake_GetLastError
#define SetLastError fake_SetLastError
#define GetCommState fake_GetCommState
#define BuildCommDCBA fake_BuildCommDCBA
#define SetCommState fake_SetCommState
#include "uart_win32_dcb.h"

typedef struct { HANDLE hPort; DCB dcb; } serial_port_windows_t;
typedef void *serial_port;
static struct { DWORD uart_speed; } g_conn;

static void uart_close(serial_port port) {
    CHECK(port != NULL);
    event('C');
    ++mock.closes;
    mock.error = ERROR_INVALID_HANDLE;
}

static void PrintAndLogEx(int level, const char *format, ...) {
    CHECK(level == WARNING);
    event('L');
    ++mock.logs;
    va_list args;
    va_start(args, format);
    vsnprintf(mock.message, sizeof(mock.message), format, args);
    va_end(args);
}

#include "production_windows_dcb.inc"

static void policy(const DCB *dcb) {
    CHECK(dcb->DCBlength == sizeof(*dcb));
    CHECK(dcb->BaudRate == 115200 && dcb->ByteSize == 8);
    CHECK(dcb->Parity == NOPARITY && dcb->StopBits == ONESTOPBIT);
    CHECK(dcb->fBinary && !dcb->fParity);
    CHECK(!dcb->fOutxCtsFlow && !dcb->fOutxDsrFlow);
    CHECK(dcb->fDtrControl == DTR_CONTROL_DISABLE && !dcb->fDsrSensitivity);
    CHECK(dcb->fTXContinueOnXoff && !dcb->fOutX && !dcb->fInX);
    CHECK(!dcb->fErrorChar && !dcb->fNull && !dcb->fAbortOnError);
    CHECK(dcb->fRtsControl == RTS_CONTROL_DISABLE && dcb->fDummy2 == 0);
    CHECK(dcb->XonChar != dcb->XoffChar);
}

static void initialization(void) {
    reset();
    DCB active;
    memset(&active, 0xa5, sizeof(active));
    const char *api = (void *)1;
    CHECK(uart_win32_initialize_dcb(PORT, &active, &api) == ERROR_SUCCESS);
    CHECK(api == NULL && mock.gets == 1 && mock.builds == 1 && mock.sets == 1);
    CHECK(strcmp(mock.order, "GBS") == 0);
    policy(&active);
    CHECK(active.XonChar == '#' && active.XoffChar == '$');
    CHECK(active.XonLim == 40 && active.XoffLim == 50);
    CHECK(active.EofChar == '\x1a' && active.ErrorChar == '?' && active.EvtChar == '!');
    CHECK(memcmp(&active, &mock.submitted[0], sizeof(active)) == 0);

    reset();
    mock.driver.XonChar = mock.driver.XoffChar = 0;
    mock.driver.DCBlength = 0;
    CHECK(uart_win32_initialize_dcb(PORT, &active, NULL) == ERROR_SUCCESS);
    CHECK(active.XonChar == '\x11' && active.XoffChar == '\x13');
    policy(&active);

    reset();
    mock.driver.XonChar = mock.driver.XoffChar = '$';
    CHECK(uart_win32_initialize_dcb(PORT, &active, NULL) == ERROR_SUCCESS);
    CHECK(active.XonChar == '\x11' && active.XoffChar == '\x13');

    reset();
    CHECK(uart_win32_initialize_dcb(PORT, NULL, &api) == ERROR_INVALID_PARAMETER);
    CHECK(strcmp(api, "DCB validation") == 0 && mock.gets == 0);
}

static void failures(void) {
    const DWORD errors[] = {ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER,
                            ERROR_DEVICE_NOT_CONNECTED, ERROR_GEN_FAILURE, ERROR_SUCCESS};
    for (unsigned phase = 0; phase < 3; ++phase) {
        for (unsigned i = 0; i < sizeof(errors) / sizeof(errors[0]); ++i) {
            reset();
            DCB active, before;
            memset(&active, 0xa5, sizeof(active));
            before = active;
            if (phase == 0) { mock.get_ok = FALSE; mock.get_error = errors[i]; }
            if (phase == 1) { mock.build_ok = FALSE; mock.build_error = errors[i]; }
            if (phase == 2) { mock.set_ok[0] = FALSE; mock.set_errors[0] = errors[i]; }
            const char *api = NULL;
            DWORD error = uart_win32_initialize_dcb(PORT, &active, &api);
            CHECK(error == (errors[i] ? errors[i] : ERROR_GEN_FAILURE));
            CHECK(memcmp(&active, &before, sizeof(active)) == 0);
            CHECK(strcmp(api, phase == 0 ? "GetCommState" : phase == 1 ? "BuildCommDCB" : "SetCommState") == 0);
            CHECK(mock.gets == 1 && mock.builds == (phase > 0) && mock.sets == (phase > 1));
            fake_SetLastError(ERROR_INVALID_HANDLE);
            CHECK(error != GetLastError());
        }
    }
}

static void baudrate(void) {
    reset();
    serial_port_windows_t port = { .hPort = PORT, .dcb = mock.driver };
    port.dcb.BaudRate = 115200;
    DCB before = port.dcb;
    mock.set_ok[0] = FALSE;
    mock.set_errors[0] = ERROR_INVALID_PARAMETER;
    CHECK(!uart_set_speed(&port, 460800));
    CHECK(GetLastError() == ERROR_INVALID_PARAMETER && g_conn.uart_speed != 460800);
    CHECK(memcmp(&before, &port.dcb, sizeof(before)) == 0);
    CHECK(uart_set_speed(&port, 230400));
    CHECK(port.dcb.BaudRate == 230400 && g_conn.uart_speed == 230400);
    CHECK(mock.gets == 0 && mock.builds == 0 && mock.sets == 2);

    reset();
    CHECK(!uart_set_speed(&port, 12345));
    CHECK(GetLastError() == ERROR_INVALID_PARAMETER && mock.sets == 0);
    CHECK(uart_win32_set_baudrate(PORT, NULL, 115200) == ERROR_INVALID_PARAMETER);
}

static void production_open(void) {
    for (unsigned silent = 0; silent < 2; ++silent) {
        for (unsigned phase = 0; phase < 3; ++phase) {
            reset();
            serial_port_windows_t port = { .hPort = PORT, .dcb = {0} };
            if (phase == 0) { mock.get_ok = FALSE; mock.get_error = ERROR_ACCESS_DENIED; }
            if (phase == 1) { mock.build_ok = FALSE; mock.build_error = ERROR_INVALID_PARAMETER; }
            if (phase == 2) { mock.set_ok[0] = FALSE; mock.set_errors[0] = ERROR_DEVICE_NOT_CONNECTED; }
            CHECK(production_open_dcb(&port, "COM3", silent) == INVALID_SERIAL_PORT);
            CHECK(mock.closes == 1 && mock.logs == !silent);
            CHECK(GetLastError() == ERROR_INVALID_HANDLE);
            if (!silent) {
                CHECK(strstr(mock.message, phase == 0 ? "GetCommState" : phase == 1 ? "BuildCommDCB" : "SetCommState"));
                CHECK(strstr(mock.message, phase == 0 ? "Win32 error 5" : phase == 1 ? "Win32 error 87" : "Win32 error 1167"));
                CHECK(mock.order[strlen(mock.order) - 2] == 'L' && mock.order[strlen(mock.order) - 1] == 'C');
            }
        }
    }
    reset();
    serial_port_windows_t port = { .hPort = PORT, .dcb = {0} };
    CHECK(production_open_dcb(&port, "COM3", false) == &port);
    CHECK(mock.closes == 0 && mock.logs == 0 && mock.sets == 1);
    policy(&port.dcb);
}

static void production_baud(void) {
    reset();
    serial_port_windows_t port = { .hPort = PORT, .dcb = mock.driver };
    port.dcb.BaudRate = 115200;
    CHECK(production_open_baud(&port, "COM3", 115200, false) == &port);
    CHECK(mock.sets == 0 && mock.closes == 0);

    reset();
    mock.set_ok[0] = FALSE;
    mock.set_errors[0] = ERROR_INVALID_PARAMETER;
    CHECK(production_open_baud(&port, "COM3", 460800, false) == &port);
    CHECK(mock.sets == 2 && mock.submitted[0].BaudRate == 460800 && mock.submitted[1].BaudRate == 115200);
    CHECK(port.dcb.BaudRate == 115200 && mock.closes == 0 && mock.logs == 0);

    for (unsigned silent = 0; silent < 2; ++silent) {
        reset();
        mock.set_ok[0] = mock.set_ok[1] = FALSE;
        mock.set_errors[0] = ERROR_INVALID_PARAMETER;
        mock.set_errors[1] = ERROR_GEN_FAILURE;
        CHECK(production_open_baud(&port, "COM3", 460800, silent) == INVALID_SERIAL_PORT);
        CHECK(mock.sets == 2 && mock.closes == 1 && mock.logs == !silent);
        if (!silent) CHECK(strstr(mock.message, "Win32 error 87; 115200 fallback error 31"));
    }
}

// BuildCommDCB is a pure parser; this probe never opens or refers to a port.
#undef BuildCommDCBA
static void actual_windows_parser(void) {
    reset();
    DCB dcb;
    memset(&dcb, 0, sizeof(dcb));
    dcb.DCBlength = sizeof(dcb);
    CHECK(BuildCommDCBA("baud=115200 parity=N data=8 stop=1", &dcb));
    CHECK(dcb.XonChar == dcb.XoffChar && !dcb.fBinary);
    CHECK(mock.gets == 0 && mock.sets == 0);
}

int main(void) {
    initialization();
    failures();
    baudrate();
    production_open();
    production_baud();
    actual_windows_parser();
    printf("Windows DCB production tests: %u scenarios, %u assertions passed; no device I/O\n", scenarios, checks);
    return 0;
}

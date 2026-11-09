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

#include <assert.h>
#include <stdio.h>
#include "comms_handshake.h"

int main(void) {
    unsigned checks = 0;
    uint8_t expected[PM3_HANDSHAKE_PING_SIZE];
    PacketResponseNG response = {.ng = true, .cmd = CMD_PING,
                                .status = PM3_SUCCESS, .length = sizeof(expected)};
    for (size_t i = 0; i < sizeof(expected); i++) expected[i] = (uint8_t)(i ^ 0xa5);
    memcpy(response.data.asBytes, expected, sizeof(expected));
#define CHECK(value) do { assert(value); checks++; } while (0)
    CHECK(pm3_handshake_ping_matches(&response, expected));
    CHECK(!pm3_handshake_ping_matches(NULL, expected));
    CHECK(!pm3_handshake_ping_matches(&response, NULL));
    response.ng = false;
    CHECK(!pm3_handshake_ping_matches(&response, expected));
    response.ng = true;
    for (uint32_t length = 0; length <= PM3_CMD_DATA_SIZE; length++) {
        response.length = (uint16_t)length;
        CHECK(pm3_handshake_ping_matches(&response, expected) == (length == sizeof(expected)));
    }
    response.length = sizeof(expected);
    for (int status = -128; status <= 127; status++) {
        response.status = (int8_t)status;
        CHECK(pm3_handshake_ping_matches(&response, expected) == (status == PM3_SUCCESS));
    }
    response.status = PM3_SUCCESS;
    for (uint32_t command = 0; command <= UINT16_MAX; command++) {
        response.cmd = (uint16_t)command;
        CHECK(pm3_handshake_ping_matches(&response, expected) == (command == CMD_PING));
    }
    response.cmd = CMD_PING;
    for (size_t i = 0; i < sizeof(expected); i++) {
        response.data.asBytes[i] ^= 1;
        CHECK(!pm3_handshake_ping_matches(&response, expected));
        response.data.asBytes[i] ^= 1;
    }
    for (size_t i = 0; i < sizeof(expected); i++) response.data.asBytes[i] = (uint8_t)i;
    CHECK(!pm3_handshake_ping_matches(&response, expected));
    printf("handshake: %u checks passed\n", checks);
    return 0;
}

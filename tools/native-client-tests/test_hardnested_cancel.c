//-----------------------------------------------------------------------------
// Copyright (C) Proxmark Studio contributors.
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//-----------------------------------------------------------------------------
// Actual scalar/SIMD search and worker coordination, without a device backend.
#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <malloc.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <windows.h>
#include "hardnested_bruteforce.h"
#include "hardnested_bf_core.h"
#include "crapto1/crapto1.h"

enum { NEVER_CANCEL, CANCEL_NOW, CANCEL_PREPARE, CANCEL_SEARCH, CANCEL_AFTER_JOIN };
static int mode;
static pthread_t coordinator;
static uint32_t assertions, scenarios;
static uint32_t allocs, frees, active_threads, creates, joins, successful_creates;
static uint32_t cpu_markers, key_markers, poll_count, fail_create_at;
static bool after_join;
static uint64_t cpu_started;
static noncelist_t nonces[256];
static noncelistentry_t test_nonces[24];
static uint8_t first_bytes[256];

#define CHECK(x) do { ++assertions; if (!(x)) { fprintf(stderr, "line %d: %s\n", __LINE__, #x); abort(); } } while (0)
#define LOAD(p) __atomic_load_n((p), __ATOMIC_ACQUIRE)

uint64_t msclock(void) { return GetTickCount64(); }
void test_msleep(uint32_t ms) { Sleep(ms); }
int num_CPUs(void) { return 3; }
int searchFile(char **path, const char *subdir, const char *name, const char *ext, bool silent) {
    (void)path; (void)subdir; (void)name; (void)ext; (void)silent;
    return -1;
}
void PrintAndLogEx(int level, const char *format, ...) { (void)level; (void)format; }
void hardnested_print_progress(uint32_t n, const char *activity, float remaining, uint64_t delay) {
    (void)n; (void)remaining; (void)delay;
    if (strstr(activity, "Hardnested CPU search started")) {
        CHECK(pthread_equal(pthread_self(), coordinator));
        ++cpu_markers;
        cpu_started = msclock();
    }
}
void hardnested_print_key_found_progress(uint32_t n, const char *key) {
    (void)n; (void)key;
    CHECK(pthread_equal(pthread_self(), coordinator));
    CHECK(!hardnested_bruteforce_stopped());
    CHECK(LOAD(&active_threads) == 0);
    ++key_markers;
}

void *test_aligned_malloc(size_t size, size_t alignment) {
    void *value = _aligned_malloc(size, alignment);
    if (value) __atomic_fetch_add(&allocs, 1, __ATOMIC_RELEASE);
    return value;
}
void test_aligned_free(void *value) {
    if (value) __atomic_fetch_add(&frees, 1, __ATOMIC_RELEASE);
    _aligned_free(value);
}
struct launch { void *(*fn)(void *); void *arg; };
static void *launch_worker(void *opaque) {
    struct launch args = *(struct launch *)opaque;
    free(opaque);
    __atomic_fetch_add(&active_threads, 1, __ATOMIC_RELEASE);
    void *result = args.fn(args.arg);
    __atomic_fetch_sub(&active_threads, 1, __ATOMIC_RELEASE);
    return result;
}
int test_pthread_create(pthread_t *thread, const pthread_attr_t *attr, void *(*fn)(void *), void *arg) {
    ++creates;
    if (fail_create_at && creates == fail_create_at) return EAGAIN;
    struct launch *launch = malloc(sizeof(*launch));
    CHECK(launch != NULL);
    launch->fn = fn; launch->arg = arg;
    int result = pthread_create(thread, attr, launch_worker, launch);
    if (result) free(launch);
    else ++successful_creates;
    return result;
}
int test_pthread_join(pthread_t thread, void **result) {
    ++joins;
    int status = pthread_join(thread, result);
    if (joins == successful_creates) after_join = true;
    return status;
}

static bool poll_cancel(void) {
    CHECK(pthread_equal(pthread_self(), coordinator));
    ++poll_count;
    switch (mode) {
        case CANCEL_NOW: return true;
        case CANCEL_PREPARE: return LOAD(&allocs) >= 3;
        case CANCEL_SEARCH: return cpu_markers && msclock() - cpu_started >= 30;
        case CANCEL_AFTER_JOIN: return after_join;
        default: return false;
    }
}

static void reset_case(int requested_mode) {
    CHECK(LOAD(&active_threads) == 0);
    CHECK(allocs == frees);
    mode = requested_mode;
    allocs = frees = creates = joins = successful_creates = 0;
    cpu_markers = key_markers = poll_count = fail_create_at = 0;
    after_join = false; cpu_started = 0;
    hardnested_bruteforce_begin(poll_cancel);
    CHECK(!hardnested_bruteforce_cancelled());
    CHECK(!hardnested_bruteforce_failed());
    CHECK(hardnested_bruteforce_workers_stopped() == 0);
    CHECK(hardnested_bruteforce_workers_at_cancel() == 0);
}

static statelist_t make_candidates(uint32_t odd_len, uint32_t even_len) {
    statelist_t values = {0};
    values.states[1] = malloc((odd_len + 1ULL) * sizeof(uint32_t));
    values.states[0] = malloc((even_len + 1ULL) * sizeof(uint32_t));
    CHECK(values.states[0] && values.states[1]);
    values.len[0] = even_len; values.len[1] = odd_len;
    for (uint32_t i = 0; i < odd_len; ++i) values.states[1][i] = i;
    for (uint32_t i = 0; i < even_len; ++i) values.states[0][i] = i;
    values.states[1][odd_len] = UINT32_MAX;
    values.states[0][even_len] = UINT32_MAX;
    return values;
}
static void free_candidates(statelist_t *values) {
    free(values->states[0]); free(values->states[1]);
}
static void synthetic_nonces(bool have_nonces) {
    memset(nonces, 0, sizeof(nonces));
    memset(test_nonces, 0, sizeof(test_nonces));
    for (unsigned i = 0; i < 256; ++i) first_bytes[i] = i;
    if (have_nonces) {
        for (unsigned i = 0; i < 24; ++i) {
            test_nonces[i].nonce_enc = 0x9278b12fu ^ (i * 0x13579bdu);
            test_nonces[i].par_enc = (i * 7) & 15;
            if (i < 8) test_nonces[i].next = i == 7 ? NULL : &test_nonces[i + 1];
            else nonces[i - 7].first = &test_nonces[i];
        }
        nonces[0].first = &test_nonces[0];
    }
    prepare_bf_test_nonces(nonces, 0);
}
static bool run(statelist_t *values, uint64_t *key) {
    bool result = brute_force_bs(NULL, values, 0x12345678, 100, (uint64_t)values->len[0] * values->len[1], nonces, first_bytes, key);
    CHECK(LOAD(&active_threads) == 0);
    CHECK(joins == successful_creates);
    CHECK(allocs == frees);
    return result;
}
static void suite(SIMDExecInstr instruction, const char *name) {
    const uint64_t sentinel = UINT64_C(0xfedcba9876543210);
    uint64_t key;
    SetSIMDInstr(instruction);
    synthetic_nonces(true);

    reset_case(CANCEL_NOW);
    statelist_t small = make_candidates(32, 64);
    key = sentinel;
    CHECK(!run(&small, &key)); CHECK(key == sentinel); CHECK(creates == 0);
    CHECK(hardnested_bruteforce_cancelled()); CHECK(key_markers == 0);
    ++scenarios;

    reset_case(NEVER_CANCEL);
    synthetic_nonces(true); key = sentinel;
    CHECK(!run(&small, &key)); CHECK(key == sentinel);
    CHECK(!hardnested_bruteforce_stopped()); CHECK(key_markers == 0);
    ++scenarios;
    free_candidates(&small);

    reset_case(CANCEL_PREPARE);
    synthetic_nonces(true);
    statelist_t preparing = make_candidates(1, 3000000);
    key = sentinel; uint64_t start = msclock();
    CHECK(!run(&preparing, &key)); CHECK(msclock() - start < 2000);
    CHECK(key == sentinel); CHECK(hardnested_bruteforce_cancelled());
    CHECK(hardnested_bruteforce_workers_at_cancel() > 0);
    CHECK(hardnested_bruteforce_workers_stopped() == 0);
    CHECK(key_markers == 0); ++scenarios;
    free_candidates(&preparing);

    reset_case(CANCEL_SEARCH);
    synthetic_nonces(true);
    statelist_t searching = make_candidates(8000000, 1024);
    key = sentinel; start = msclock();
    CHECK(!run(&searching, &key)); CHECK(msclock() - start < 2000);
    CHECK(key == sentinel); CHECK(hardnested_bruteforce_cancelled());
    CHECK(hardnested_bruteforce_workers_at_cancel() > 0);
    CHECK(hardnested_bruteforce_workers_stopped() > 0);
    CHECK(cpu_markers == 1); CHECK(key_markers == 0); ++scenarios;

    // Cancellation remains sticky until a new command explicitly starts.
    key = sentinel; uint32_t before_creates = creates;
    CHECK(!run(&searching, &key)); CHECK(key == sentinel);
    CHECK(creates == before_creates); ++scenarios;

    reset_case(NEVER_CANCEL);
    synthetic_nonces(true); fail_create_at = 2; key = sentinel;
    CHECK(!run(&searching, &key)); CHECK(key == sentinel);
    CHECK(hardnested_bruteforce_failed()); CHECK(!hardnested_bruteforce_cancelled());
    CHECK(successful_creates == 1); CHECK(joins == 1); CHECK(key_markers == 0);
    ++scenarios;
    free_candidates(&searching);

    reset_case(NEVER_CANCEL);
    synthetic_nonces(false);
    statelist_t winner = make_candidates(1, 1);
    key = sentinel;
    CHECK(run(&winner, &key)); CHECK(key != sentinel); CHECK(key_markers == 1);
    CHECK(!hardnested_bruteforce_stopped()); ++scenarios;

    reset_case(CANCEL_AFTER_JOIN);
    synthetic_nonces(false); key = sentinel;
    CHECK(!run(&winner, &key)); CHECK(key == sentinel); CHECK(key_markers == 0);
    CHECK(hardnested_bruteforce_cancelled());
    CHECK(hardnested_bruteforce_workers_at_cancel() == 0);
    CHECK(hardnested_bruteforce_workers_stopped() == 0); ++scenarios;

    reset_case(NEVER_CANCEL);
    synthetic_nonces(false); key = sentinel;
    CHECK(run(&winner, &key)); CHECK(key != sentinel); CHECK(key_markers == 1);
    CHECK(!hardnested_bruteforce_stopped()); ++scenarios;
    free_candidates(&winner);
    printf("%s: passed\n", name);
}
int main(void) {
    coordinator = pthread_self();
    suite(SIMD_NONE, "scalar");
#if defined(COMPILER_HAS_SIMD_X86)
    __builtin_cpu_init();
    if (__builtin_cpu_supports("mmx")) suite(SIMD_MMX, "MMX");
    if (__builtin_cpu_supports("sse2")) suite(SIMD_SSE2, "SSE2");
    if (__builtin_cpu_supports("avx")) suite(SIMD_AVX, "AVX");
    if (__builtin_cpu_supports("avx2")) suite(SIMD_AVX2, "AVX2");
#endif
#if defined(COMPILER_HAS_SIMD_AVX512)
    if (__builtin_cpu_supports("avx512f")) suite(SIMD_AVX512, "AVX512");
#endif
    printf("%u scenarios, %u assertions passed\n", scenarios, assertions);
    return 0;
}

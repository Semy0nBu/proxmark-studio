#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static unsigned long checks;
#define CHECK(x) do { __atomic_add_fetch(&checks, 1, __ATOMIC_SEQ_CST); if (!(x)) { \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x); exit(1); \
} } while (0)

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t changed;
    bool pending, dead;
    size_t length;
    unsigned waiters, accepted;
} queue_t;
typedef struct { queue_t *queue; bool waited; int result; } sender_t;
static pthread_key_t sender_key;

/* Instrument the real pthread wait, not a copy of the production gate. */
static int tracked_wait(pthread_cond_t *changed, pthread_mutex_t *mutex) {
    sender_t *sender = pthread_getspecific(sender_key);
    if (sender && !sender->waited) {
        sender->waited = true;
        __atomic_add_fetch(&sender->queue->waiters, 1, __ATOMIC_SEQ_CST);
    }
    return pthread_cond_wait(changed, mutex);
}
#define pthread_cond_wait tracked_wait
#include "tx_lifecycle.h"
#undef pthread_cond_wait

static uint64_t now_ms(void) {
    struct timespec t;
    CHECK(clock_gettime(CLOCK_MONOTONIC, &t) == 0);
    return (uint64_t)t.tv_sec * 1000 + (uint64_t)t.tv_nsec / 1000000;
}
static void pause_ms(void) {
    struct timespec t = {0, 1000000};
    nanosleep(&t, NULL);
}
static void wait_unsigned(const unsigned *value, unsigned expected) {
    uint64_t end = now_ms() + 2500;
    while (__atomic_load_n(value, __ATOMIC_SEQ_CST) < expected) {
        CHECK(now_ms() < end); pause_ms();
    }
}
static void wait_result(const int *value) {
    uint64_t end = now_ms() + 2500;
    while (!__atomic_load_n(value, __ATOMIC_SEQ_CST)) {
        CHECK(now_ms() < end); pause_ms();
    }
}
static void init(queue_t *q, bool pending) {
    *q = (queue_t){0}; q->pending = pending; q->length = pending ? 42 : 0;
    CHECK(pthread_mutex_init(&q->mutex, NULL) == 0);
    CHECK(pthread_cond_init(&q->changed, NULL) == 0);
}
static void destroy(queue_t *q) {
    CHECK(pthread_cond_destroy(&q->changed) == 0);
    CHECK(pthread_mutex_destroy(&q->mutex) == 0);
}
static void stop(queue_t *q) {
    pm3_tx_stop(&q->mutex, &q->changed, &q->pending, &q->length, &q->dead);
}
static void *send_one(void *context) {
    sender_t *sender = context; queue_t *q = sender->queue;
    CHECK(pthread_setspecific(sender_key, sender) == 0);
    if (!pm3_tx_lock_ready(&q->mutex, &q->changed, &q->pending, &q->dead)) {
        __atomic_store_n(&sender->result, -1, __ATOMIC_SEQ_CST);
        return NULL;
    }
    CHECK(!q->dead && !q->pending);
    /* Production caller owns the mutex while submitting a new request. */
    CHECK(pthread_mutex_trylock(&q->mutex) == EBUSY);
    ++q->accepted; q->pending = true; q->length = 24;
    pthread_mutex_unlock(&q->mutex);
    __atomic_store_n(&sender->result, 1, __ATOMIC_SEQ_CST);
    return NULL;
}

static void test_all_waiters_exit_on_dead_stream(void) {
    queue_t q; sender_t senders[8]; pthread_t threads[8]; unsigned i;
    init(&q, true);
    for (i = 0; i < 8; ++i) {
        senders[i] = (sender_t){&q, false, 0};
        CHECK(pthread_create(&threads[i], NULL, send_one, &senders[i]) == 0);
    }
    wait_unsigned(&q.waiters, 8); /* All eight actually entered cond_wait. */
    for (i = 0; i < 8; ++i) CHECK(__atomic_load_n(&senders[i].result, __ATOMIC_SEQ_CST) == 0);
    stop(&q);
    for (i = 0; i < 8; ++i) {
        wait_result(&senders[i].result);
        CHECK(pthread_join(threads[i], NULL) == 0);
        CHECK(senders[i].result == -1);
    }
    CHECK(q.dead && !q.pending && !q.length && !q.accepted);
    for (i = 0; i < 16; ++i) {
        CHECK(!pm3_tx_lock_ready(&q.mutex, &q.changed, &q.pending, &q.dead));
        CHECK(pthread_mutex_trylock(&q.mutex) == 0); /* Failure unlocked it. */
        pthread_mutex_unlock(&q.mutex);
    }
    CHECK(q.waiters == 8);
    destroy(&q);
}

static void test_normal_completion_allows_next_command(void) {
    queue_t q; sender_t sender; pthread_t thread;
    init(&q, true); sender = (sender_t){&q, false, 0};
    CHECK(pthread_create(&thread, NULL, send_one, &sender) == 0);
    wait_unsigned(&q.waiters, 1);
    pthread_mutex_lock(&q.mutex);
    q.pending = false; q.length = 0;
    pthread_cond_signal(&q.changed);
    pthread_mutex_unlock(&q.mutex);
    wait_result(&sender.result);
    CHECK(pthread_join(thread, NULL) == 0);
    CHECK(sender.result == 1 && q.accepted == 1 && q.pending && q.length == 24 && !q.dead);
    stop(&q); destroy(&q);
}

typedef struct { queue_t *queue; unsigned started; } stopper_t;
static void *stop_thread(void *context) {
    stopper_t *s = context;
    __atomic_store_n(&s->started, 1, __ATOMIC_SEQ_CST);
    stop(s->queue);
    return NULL;
}
static void test_death_and_submission_share_mutex(void) {
    queue_t q; stopper_t s; pthread_t thread;
    init(&q, false); s = (stopper_t){&q, 0};
    CHECK(pm3_tx_lock_ready(&q.mutex, &q.changed, &q.pending, &q.dead));
    CHECK(pthread_create(&thread, NULL, stop_thread, &s) == 0);
    wait_unsigned(&s.started, 1);
    CHECK(!__atomic_load_n(&q.dead, __ATOMIC_SEQ_CST));
    q.pending = true; q.length = 128;
    pthread_mutex_unlock(&q.mutex);
    CHECK(pthread_join(thread, NULL) == 0);
    CHECK(q.dead && !q.pending && !q.length);

    /* The next manually opened connection explicitly clears the old state. */
    pthread_mutex_lock(&q.mutex);
    __atomic_store_n(&q.dead, false, __ATOMIC_SEQ_CST);
    pthread_mutex_unlock(&q.mutex);
    CHECK(pm3_tx_lock_ready(&q.mutex, &q.changed, &q.pending, &q.dead));
    pthread_mutex_unlock(&q.mutex);
    stop(&q); destroy(&q);
}

int main(void) {
    CHECK(pthread_key_create(&sender_key, NULL) == 0);
    test_all_waiters_exit_on_dead_stream();
    test_normal_completion_allows_next_command();
    test_death_and_submission_share_mutex();
    CHECK(pthread_key_delete(sender_key) == 0);
    printf("Production TX lifecycle: real pthread wait/broadcast/death/restart checks passed (%lu checks); no hardware used\n", checks);
    return 0;
}

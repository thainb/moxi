/* -*- Mode: C; tab-width: 4; c-basic-offset: 4; indent-tabs-mode: nil -*- */

#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <sysexits.h>
#include <pthread.h>
#include <assert.h>
#include <libmemcached/memcached.h>
#include "memcached.h"
#include "cproxy.h"
#include "work.h"
#include "agent.h"

// From libmemcached.
//
memcached_return memcached_connect(memcached_server_st *ptr);
memcached_return memcached_version(memcached_st *ptr);

#define DEFAULT_LATENCY_ITERATIONS 5

// Local declarations.
//
void ping_server(char *server_name,
                 int latency_iterations,
                 proxy_behavior *behavior,
                 void *opaque,
                 conflate_add_ping_report add_report);

void on_conflate_ping_test(void *userdata, void *opaque,
                           kvpair_t *form,
                           conflate_add_ping_report add_report) {
    assert(userdata);
    assert(add_report);

    // The form key-multivalues looks roughly like...
    //
    //  servers
    //    svrname1
    //    svrname2
    //  svr-svrname1
    //    host=mc1.foo.net
    //    port=11211
    //    bucket=buck1
    //    usr=test1
    //    pwd=password
    //  svr-svrname2
    //    host=mc2.foo.net
    //    port=11211
    //    bucket=buck1
    //    usr=test1
    //    pwd=password
    //
    if (form != NULL) {
        char   server_key[200];
        char **servers = get_key_values(form, "servers");

        int latency_iterations = DEFAULT_LATENCY_ITERATIONS;
        kvpair_t *l_iter = find_kvpair(form, "latency_iterations");
        if (l_iter) {
            if (!safe_strtol(l_iter->values[0], &latency_iterations)) {
                latency_iterations = DEFAULT_LATENCY_ITERATIONS;
            }
        }

        for (int j = 0; servers != NULL && servers[j]; j++) {
            snprintf(server_key, sizeof(server_key),
                     "svr-%s", servers[j]);

            if (settings.verbose > 1)
                fprintf(stderr, "ping_test %s\n",
                        server_key);

            proxy_behavior behavior;

            memset(&behavior, 0, sizeof(behavior));

            char **props = get_key_values(form, server_key);
            for (int k = 0; props && props[k]; k++) {
                cproxy_parse_behavior_key_val_str(props[k],
                                                  &behavior);
            }

            ping_server(servers[j], latency_iterations,
                        &behavior, opaque, add_report);
        }
    }

    add_report(opaque, NULL, NULL);
}

void ping_server(char *server_name,
                 int latency_iterations,
                 proxy_behavior *behavior,
                 void *opaque,
                 conflate_add_ping_report add_report) {
    assert(server_name);
    assert(behavior);
    assert(add_report);

    if (strlen(behavior->host) <= 0 ||
        behavior->port <= 0)
        return;

    memcached_st         mst;
    memcached_server_st *mservers;

    struct timeval timing;

    char  buf[300] = { 0x00 };
    char *bufa[2]  = { buf, NULL };

    kvpair_t *kvr = NULL, *kvtmp = NULL;

#define dbl_report(name, dval)                  \
    snprintf(buf, sizeof(buf), "%f", dval);     \
    kvtmp = mk_kvpair(name, bufa);              \
    kvtmp->next = kvr;                          \
    kvr = kvtmp;

#define tv_report(name, mark, val)                  \
    timeval_subtract(&timing, &val, &mark);         \
    dbl_report(name, timeval_to_double(timing));

    if (memcached_create(&mst) != NULL) {
        memcached_behavior_set(&mst, MEMCACHED_BEHAVIOR_NO_BLOCK, 1);

        snprintf(buf, sizeof(buf),
                 "%s:%u",
                 behavior->host,
                 behavior->port);

        mservers = memcached_servers_parse(buf);
        if (mservers != NULL) {
            memcached_server_push(&mst, mservers);
            memcached_server_list_free(mservers);
            mservers = NULL;

            int nconns = memcached_server_count(&mst);
            bool connected  = false;

            for (int i = 0; i < nconns; i++) {
                if (settings.verbose > 1)
                    fprintf(stderr, "ping_test connecting %d\n", i);

                struct timeval start;
                gettimeofday(&start, NULL);

                memcached_return rc = memcached_connect(&mst.hosts[i]);
                if (rc == MEMCACHED_SUCCESS) {
                    struct timeval tv_conn;
                    gettimeofday(&tv_conn, NULL);
                    tv_report("conn", start, tv_conn);

                    if (cproxy_auth_downstream(&mst.hosts[i],
                                               behavior) &&
                        cproxy_bucket_downstream(&mst.hosts[i],
                                                 behavior)) {
                        struct timeval tv_auth;
                        gettimeofday(&tv_auth, NULL);
                        tv_report("auth", tv_conn, tv_auth);

                        // Flag whether to proceed if we connected
                        connected = true;
                    }
                }
            }

            if (connected) {

                // TODO: Need a better ping test here.
                // TODO: Set a few small & big values, and get them.
                double v_results[latency_iterations];
                for (int i = 0; i < latency_iterations; i++) {
                    struct timeval tv_version_pre;
                    gettimeofday(&tv_version_pre, NULL);

                    memcached_version(&mst);

                    struct timeval tv_version_post;
                    gettimeofday(&tv_version_post, NULL);
                    timeval_subtract(&timing, &tv_version_post,
                                     &tv_version_pre);
                    v_results[i] = timeval_to_double(timing);
                }

                struct moxi_stats version_stats = { 0.0 };
                compute_stats(&version_stats, v_results, latency_iterations);
                dbl_report("version_min", version_stats.min);
                dbl_report("version_avg", version_stats.avg);
                dbl_report("version_max", version_stats.max);
                dbl_report("version_stddev", version_stats.stddev);
            }
        }

        if (!kvr) {
            snprintf(buf, sizeof(buf), "Didn't work. :(");
            kvr = mk_kvpair("error", bufa);
        }

        assert(kvr);
        add_report(opaque, server_name, kvr);
        free_kvpair(kvr);

        memcached_free(&mst);
    }
}


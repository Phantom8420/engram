import os
from functools import lru_cache

from falkordb import FalkorDB
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry

from engram import graph_name

LIVE = graph_name()


@lru_cache(maxsize=1)
def connect():
    # one client per process; a hosted DB drops idle sockets, so health-check them and retry a dropped call
    # username/password are only set for a hosted FalkorDB (e.g. FalkorDB Cloud behind a Vercel deploy)
    return FalkorDB(host=os.environ.get("FALKORDB_HOST", "localhost"), port=int(os.environ.get("FALKORDB_PORT", 6379)),
                    username=os.environ.get("FALKORDB_USERNAME"), password=os.environ.get("FALKORDB_PASSWORD"),
                    retry=Retry(ExponentialBackoff(cap=2), 3), retry_on_error=[ConnectionError, TimeoutError],
                    health_check_interval=30, socket_connect_timeout=10)


def graph(name=LIVE):
    return connect().select_graph(name)


def rows(g, q, **params):
    return g.query(q, params).result_set


def batched(g, q, items, size=500):
    for i in range(0, len(items), size):
        g.query(q, {"rows": items[i:i + size]})

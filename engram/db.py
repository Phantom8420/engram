import os

from falkordb import FalkorDB

from engram import graph_name

LIVE = graph_name()


def connect():
    # username/password are only set for a hosted FalkorDB (e.g. FalkorDB Cloud behind a Vercel deploy)
    return FalkorDB(host=os.environ.get("FALKORDB_HOST", "localhost"), port=int(os.environ.get("FALKORDB_PORT", 6379)),
                    username=os.environ.get("FALKORDB_USERNAME"), password=os.environ.get("FALKORDB_PASSWORD"))


def graph(name=LIVE):
    return connect().select_graph(name)


def rows(g, q, **params):
    return g.query(q, params).result_set


def batched(g, q, items, size=500):
    for i in range(0, len(items), size):
        g.query(q, {"rows": items[i:i + size]})

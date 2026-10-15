import os

from falkordb import FalkorDB

LIVE = "engram"


def graph(name=LIVE):
    db = FalkorDB(host=os.environ.get("FALKORDB_HOST", "localhost"), port=int(os.environ.get("FALKORDB_PORT", 6379)))
    return db.select_graph(name)


def rows(g, q, **params):
    return g.query(q, params).result_set


def batched(g, q, items, size=500):
    for i in range(0, len(items), size):
        g.query(q, {"rows": items[i:i + size]})

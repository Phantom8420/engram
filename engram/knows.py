"""Derived graph state: who KNOWS which module, and how important each module is."""
from engram.db import LIVE, graph, rows

DECAY_SECONDS = 365 * 86400  # knowledge halves in ~8 months without touching the module

KNOWS = """
MATCH (p:Person)-[a:AUTHORED|REVIEWED|COMMENTED]->(pr:PR)-[:TOUCHES]->(m:Module)
WHERE pr.created_at <= $asof AND m.hub = false
WITH p, m, max(pr.created_at) AS last,
     sum(CASE type(a) WHEN 'AUTHORED' THEN 1.0 WHEN 'REVIEWED' THEN 0.5 ELSE 0.2 END
         * exp(-toFloat($asof - pr.created_at) / $decay)) AS s
WHERE s > 0.05
MERGE (p)-[k:KNOWS]->(m) SET k.score = s, k.last_seen = last
"""

# OWNS is the gatekeeper side: who pressed merge on changes to a module in the last two years, as a share of them
OWNS = """
MATCH (p:Person)-[:MERGED]->(pr:PR)-[:TOUCHES]->(m:Module)
WHERE pr.created_at <= $asof AND pr.created_at > $asof - 2 * 365 * 86400 AND m.hub = false
WITH m, p, count(pr) AS n
WITH m, collect([p, n]) AS ps, sum(n) AS total
UNWIND ps AS x
WITH m, x[0] AS p, x[1] AS n, total WHERE total >= 3 AND toFloat(n) / total >= 0.25
MERGE (p)-[o:OWNS]->(m) SET o.merges = n, o.share = toFloat(n) / total
"""


def asof_now(g):
    return rows(g, "MATCH (pr:PR) RETURN max(pr.created_at)")[0][0]


def derive(g, asof=None):
    asof = asof or asof_now(g)
    g.query("MATCH ()-[k:KNOWS]->() DELETE k")
    g.query(KNOWS, {"asof": asof, "decay": float(DECAY_SECONDS)})
    g.query("MATCH ()-[o:OWNS]->() DELETE o")
    g.query(OWNS, {"asof": asof})
    importance(g, asof)
    return asof


def importance(g, asof):
    """0.5 * PageRank over CO_CHANGES + 0.3 * PRs in the last year + 0.2 * active decisions, each scaled to 0..1."""
    pr = dict(rows(g, "CALL algo.pageRank('Module', 'CO_CHANGES') YIELD node, score RETURN node.path, score"))
    recent = dict(rows(g, """MATCH (m:Module) OPTIONAL MATCH (m)<-[:TOUCHES]-(x:PR) WHERE x.created_at > $since
                            RETURN m.path, count(x)""", since=asof - 365 * 86400))
    dec = dict(rows(g, """MATCH (m:Module) OPTIONAL MATCH (m)<-[:AFFECTS]-(d:Decision) WHERE d.valid_to IS NULL
                         RETURN m.path, count(d)"""))
    scale = lambda d: {k: v / (max(d.values()) or 1) for k, v in d.items()}
    pr, recent, dec = scale(pr), scale(recent), scale(dec)
    out = [{"path": m, "imp": round(0.5 * pr.get(m, 0) + 0.3 * recent.get(m, 0) + 0.2 * dec.get(m, 0), 4)} for m in recent]
    g.query("UNWIND $rows AS r MATCH (m:Module {path:r.path}) SET m.importance = r.imp", {"rows": out})


if __name__ == "__main__":
    g = graph(LIVE)
    derive(g)
    print(rows(g, "MATCH ()-[k:KNOWS]->() RETURN count(k)"))
    print(rows(g, "MATCH (m:Module {kind:'code'}) RETURN m.path, m.importance ORDER BY m.importance DESC LIMIT 8"))

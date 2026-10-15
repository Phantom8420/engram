"""The Future lens: fork the live graph (GRAPH.COPY), apply a hypothetical in the fork, rerun the same curated queries
there and diff against the live graph. The live graph is never written to."""
import sys
import time
import uuid

from engram import tools as T
from engram.db import graph, rows
from engram.loss import HOLDER


def fork(g):
    name = f"fork_{uuid.uuid4().hex[:8]}"
    g.copy(name)
    return graph(name)


def holders(g):
    return {m: (n, top) for m, n, top in rows(g, """
        MATCH (p:Person)-[k:KNOWS]->(m:Module {kind:'code'}) WHERE k.score >= $h
        WITH m, p, k ORDER BY k.score DESC
        RETURN m.path, count(p), collect(p.handle)[0]""", h=HOLDER)}


def leave(g, handle):
    """What breaks if @handle stops contributing tomorrow."""
    f, p = fork(g), T.Path()
    try:
        f.query("MATCH (:Person {handle:$h})-[k:KNOWS]->() DELETE k", {"h": handle})
        before, after = holders(g), holders(f)
        hit = {m: (before[m][0], after.get(m, (0, None))) for m in before
               if before[m][0] > after.get(m, (0, None))[0] and after.get(m, (0, None))[0] <= 1}
        who = p.node("Person", handle)
        orphaned = []
        for m, (n0, (n1, heir)) in sorted(hit.items(), key=lambda x: x[1][1][0]):
            mid = p.node("Module", m)
            p.edge(who, mid, "KNOWS")
            if heir:
                p.edge(p.node("Person", heir), mid, "KNOWS")
            orphaned.append({"module": m, "holders_before": n0, "holders_after": n1, "heir": heir})
        mods = list(hit)
        decisions = rows(f, """UNWIND $mods AS mp MATCH (d:Decision)-[:AFFECTS]->(m:Module {path:mp}) WHERE d.valid_to IS NULL
                               WITH d.source_url AS u, collect(d)[0] AS d, collect(m.path)[0] AS mp
                               RETURN d.id, d.summary, u, mp LIMIT 12""", mods=mods)
        docs = rows(f, """UNWIND $mods AS mp MATCH (doc:Doc)-[:DESCRIBES]->(m:Module {path:mp})
                          RETURN DISTINCT doc.path, m.path""", mods=mods)
    finally:
        f.delete()
    for did, summary, _, m in decisions:
        p.edge(p.node("Decision", did, summary), f"Module:{m}", "AFFECTS")
    for d, m in docs:
        p.edge(p.node("Module", d), f"Module:{m}", "DESCRIBES")
    lines = [f"{o['module']}: {o['holders_before']} -> {o['holders_after']} people who can answer"
             + (f" (only @{o['heir']} left)" if o["heir"] else " (nobody left)") for o in orphaned]
    answer = (f"If @{handle} leaves:\n" + "\n".join(lines)
              + f"\nActive decisions on those modules, now with one defender or none: {len(decisions)}\n"
              + "\n".join(f"    - {s} {u}" for _, s, u, _ in decisions[:4])
              + (f"\nDocs with nobody left to keep them current: {', '.join(sorted({d for d, _ in docs}))}" if docs else "")
              ) if orphaned else f"If @{handle} leaves, every module they know still has at least two people who can answer."
    return {"answer": answer, "data": {"orphaned": orphaned, "decisions": [list(d) for d in decisions],
                                       "docs": [list(d) for d in docs]}, "path": p.dump()}


def reverse(g, decision_id):
    """What has to change if decision_id were reversed today."""
    f, p = fork(g), T.Path()
    try:
        now = int(time.time())
        found = rows(f, "MATCH (d:Decision {id:$d}) RETURN d.summary, d.valid_from", d=decision_id)
        if not found:
            return {"answer": f"No decision {decision_id}.", "data": {}, "path": p.dump()}
        summary, at = found[0]
        # the reversal is a new decision, made now, on the same modules; the old one stops being in force
        f.query("""MATCH (d:Decision {id:$d})
                   CREATE (r:Decision {id:'reversal', summary:'Reversal of ' + d.summary, topic:d.topic,
                           valid_from:$now, status:'active'})-[:SUPERSEDES]->(d)
                   SET d.valid_to = $now, d.status = 'superseded'
                   WITH d, r MATCH (d)-[:AFFECTS]->(m:Module) CREATE (r)-[:AFFECTS]->(m)""", {"d": decision_id, "now": now})
        stale_after = {o["doc"] for o in T.stale_docs(f)["data"]}
        built_on = rows(f, """MATCH (d:Decision {id:$d})-[:AFFECTS]->(m:Module)<-[:AFFECTS]-(d2:Decision)
                              WHERE d2.valid_from > $at AND d2.valid_to IS NULL AND d2.id <> 'reversal'
                              RETURN DISTINCT d2.id, d2.summary, d2.source_url, m.path LIMIT 8""", d=decision_id, at=at)
        revived = rows(f, """MATCH (:Decision {id:$d})-[:SUPERSEDES]->(old:Decision)
                             RETURN old.id, old.summary, old.source_url""", d=decision_id)
        mods = [m for (m,) in rows(f, "MATCH (:Decision {id:$d})-[:AFFECTS]->(m:Module) RETURN m.path", d=decision_id)]
        doc_hits = rows(f, """UNWIND $mods AS mp MATCH (doc:Doc)-[:DESCRIBES]->(m:Module {path:mp})
                              RETURN DISTINCT doc.path, m.path""", mods=mods)
        ask = rows(f, """UNWIND $mods AS mp MATCH (p:Person)-[k:KNOWS]->(:Module {path:mp})
                         RETURN p.handle, sum(k.score) AS s ORDER BY s DESC LIMIT 3""", mods=mods)
    finally:
        f.delete()
    stale_now = {o["doc"] for o in T.stale_docs(g)["data"]}
    d0 = p.node("Decision", decision_id, summary)
    for m in mods:
        p.edge(d0, p.node("Module", m), "AFFECTS")
    for did, s, _, m in built_on:
        p.edge(p.node("Decision", did, s), f"Module:{m}", "AFFECTS")
    for did, s, _ in revived:
        p.edge(d0, p.node("Decision", did, s), "SUPERSEDES")
    for doc, m in doc_hits:
        p.edge(p.node("Module", doc), f"Module:{m}", "DESCRIBES")
    newly_stale = sorted(stale_after - stale_now) or sorted({d for d, _ in doc_hits})
    answer = (f"If \"{summary}\" were reversed today:\n"
              f"Docs to rewrite: {', '.join(newly_stale) or 'none'}\n"
              f"Later decisions built on top of it, to re-check: {len(built_on)}"
              + "".join(f"\n    - {s} {u}" for _, s, u, _ in built_on[:4])
              + "".join(f"\nComes back into force: {s} {u}" for _, s, u in revived)
              + (f"\nAsk first: {', '.join('@' + h for h, _ in ask)}" if ask else ""))
    return {"answer": answer, "data": {"docs": newly_stale, "built_on": [list(b) for b in built_on],
                                       "revived": [list(r) for r in revived], "ask": [h for h, _ in ask]},
            "path": p.dump()}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    g = graph()
    kind, target = sys.argv[1], sys.argv[2]
    print((leave if kind == "leave" else reverse)(g, target)["answer"])

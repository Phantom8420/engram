"""The brain's tools. Each one is a curated, parameterised Cypher query that returns an answer plus the
graph path behind it (nodes + edges), so the UI can light up exactly what the answer rests on."""
import re

from engram import REPO
from engram.db import rows
from engram.decisions import topic_of
from engram.knows import asof_now

ROW_LIMIT = 10
PROJECT = REPO.split("/")[1]  # every issue says "black"; that names the project, not black.py


class Path:
    def __init__(self):
        self.nodes, self.edges = {}, set()

    def node(self, label, key, name=None):
        nid = f"{label}:{key}"
        self.nodes.setdefault(nid, {"id": nid, "label": label, "name": str(name or key)})
        return nid

    def edge(self, a, b, kind):
        self.edges.add((a, b, kind))

    def dump(self):
        return {"nodes": list(self.nodes.values()), "edges": [{"source": a, "target": b, "type": t} for a, b, t in self.edges]}


def day(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") if t else "?"


def find_modules(g, text, limit=3):
    """Modules named in free text: by file stem (linegen, trans) or path fragment, most important first."""
    words = {w.lower().rstrip(".") for w in re.findall(r"[A-Za-z_][\w./-]{3,}", text)}
    words |= {w.rsplit("/", 1)[-1].rsplit(".", 1)[0] for w in words}
    mods = rows(g, "MATCH (m:Module) WHERE m.hub = false AND m.kind IN ['code', 'doc'] RETURN m.path, m.importance, m.kind")
    hits = []
    for path, imp, kind in mods:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        if path.lower() in words or (len(stem) >= 4 and stem != PROJECT and stem in words):
            hits.append((kind == "code", imp or 0, path))
    return [p for *_, p in sorted(hits, reverse=True)[:limit]]


def who_knows(g, module):
    p = Path()
    m = p.node("Module", module)
    out = []
    for handle, score, last, ev in rows(g, """
            MATCH (p:Person)-[k:KNOWS]->(m:Module {path:$m})
            OPTIONAL MATCH (p)-[:AUTHORED|REVIEWED]->(pr:PR)-[:TOUCHES]->(m)
            WITH p, k, pr ORDER BY pr.created_at DESC
            WITH p, k, collect(pr)[..2] AS ev
            RETURN p.handle, k.score, k.last_seen, [e IN ev | [e.number, e.title, e.url]]
            ORDER BY k.score DESC LIMIT 5""", m=module):
        who = p.node("Person", handle)
        p.edge(who, m, "KNOWS")
        for n, title, url in ev:
            pr = p.node("PR", n, f"#{n} {title}")
            p.edge(who, pr, "AUTHORED")
            p.edge(pr, m, "TOUCHES")
        out.append({"handle": handle, "score": round(score, 2), "last_seen": day(last),
                    "evidence": [{"number": n, "title": t, "url": u} for n, t, u in ev]})
    lines = [f"@{o['handle']} (score {o['score']}, last touched {o['last_seen']})"
             + "".join(f"\n    - #{e['number']} {e['title']} {e['url']}" for e in o["evidence"]) for o in out]
    answer = f"Who actually knows {module}:\n" + "\n".join(lines) if out else f"Nobody has worked on {module} recently."
    return {"answer": answer, "data": out, "path": p.dump()}


def why(g, module=None, topic=None):
    p = Path()
    where = "MATCH (d:Decision)-[:AFFECTS]->(:Module {path:$k})" if module else "MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$k})"
    res = rows(g, where + """
        MATCH (d)-[:DECIDED_IN]->(pr:PR)
        OPTIONAL MATCH (d)-[:SUPERSEDES]->(old:Decision)
        OPTIONAL MATCH (d)-[:BY]->(who:Person)
        RETURN d.id, d.summary, d.quote, d.status, d.valid_from, pr.number, pr.url, who.handle, collect(old.id),
               CASE WHEN d.why IS NULL THEN 1 ELSE 0 END AS no_reason
        ORDER BY no_reason, d.valid_from DESC LIMIT 6""", k=module or topic)
    anchor = p.node("Module", module) if module else p.node("Topic", topic)
    out = []
    for did, summary, quote, status, at, n, url, handle, olds, _ in res:
        d = p.node("Decision", did, summary)
        p.edge(d, anchor, "AFFECTS" if module else "ABOUT")
        pr = p.node("PR", n, f"#{n}")
        p.edge(d, pr, "DECIDED_IN")
        if handle:
            p.edge(d, p.node("Person", handle), "BY")
        for o in olds:
            p.edge(d, p.node("Decision", o), "SUPERSEDES")
        out.append({"id": did, "summary": summary, "quote": quote, "status": status, "date": day(at),
                    "pr": n, "url": url, "by": handle, "supersedes": olds})
    subject = module or f"topic '{topic}'"
    lines = [f"{o['date']} [{o['status']}] {o['summary']}\n    \"{o['quote']}\" — @{o['by'] or '?'} {o['url']}"
             + (f"\n    supersedes {', '.join(o['supersedes'])}" if o["supersedes"] else "") for o in out]
    answer = f"Why {subject} is the way it is:\n" + "\n".join(lines) if out else f"No recorded decisions about {subject}."
    return {"answer": answer, "data": out, "path": p.dump()}


def stale_docs(g):
    p = Path()
    res = rows(g, """
        MATCH (doc:Doc)-[:DESCRIBES]->(m:Module)<-[:AFFECTS]-(d:Decision)
        WHERE d.valid_from > doc.updated_at AND d.valid_to IS NULL
        WITH doc, m, d ORDER BY d.valid_from DESC
        WITH doc, collect(DISTINCT m.path)[..3] AS mods, collect(DISTINCT [d.summary, d.source_url])[..3] AS ds,
             count(DISTINCT d) AS n
        RETURN doc.path, doc.updated_at, mods, ds, n ORDER BY n DESC LIMIT $lim""", lim=ROW_LIMIT)
    out = []
    for path, updated, mods, ds, n in res:
        doc = p.node("Module", path)
        for mp in mods:
            p.edge(doc, p.node("Module", mp), "DESCRIBES")
        for summary, url in ds:
            dn = p.node("Decision", url, summary)
            for mp in mods:
                p.edge(dn, f"Module:{mp}", "AFFECTS")
        out.append({"doc": path, "last_edited": day(updated), "newer_decisions": n, "modules": mods,
                    "examples": [{"summary": s, "url": u} for s, u in ds]})
    lines = [f"{o['doc']} (last edited {o['last_edited']}): {o['newer_decisions']} newer decisions on {', '.join(o['modules'])}"
             + "".join(f"\n    - {e['summary']} {e['url']}" for e in o["examples"]) for o in out]
    return {"answer": "Docs that are probably out of date:\n" + "\n".join(lines) if out else "No stale docs found.",
            "data": out, "path": p.dump()}


def route_issue(g, text):
    """Suggest reviewers for an issue: modules it names (or modules shaped by decisions on its topic) -> active knowers."""
    p = Path()
    mods, via = find_modules(g, text, 4), "named in the issue"
    topic = topic_of(text)
    if not mods and topic != "other":
        via = f"shaped by decisions on '{topic}'"
        mods = [m for m, _ in rows(g, """MATCH (:Topic {name:$t})<-[:ABOUT]-(:Decision)-[:AFFECTS]->(m:Module {kind:'code'})
                                         RETURN m.path, count(*) AS c ORDER BY c DESC LIMIT 3""", t=topic)]
    if not mods:
        return {"answer": "I couldn't tie this issue to any module or topic.", "data": [], "path": p.dump()}
    since = asof_now(g) - 365 * 86400
    res = rows(g, """UNWIND $mods AS mp MATCH (m:Module {path:mp})<-[k:KNOWS]-(p:Person) WHERE k.last_seen > $since
                     RETURN p.handle, sum(k.score) AS fit, collect(m.path) ORDER BY fit DESC LIMIT 3""", mods=mods, since=since)
    out = []
    for handle, fit, via_mods in res:
        who = p.node("Person", handle)
        for mp in via_mods:
            p.edge(who, p.node("Module", mp), "KNOWS")
        out.append({"handle": handle, "fit": round(fit, 2), "modules": via_mods})
    lines = [f"@{o['handle']} (fit {o['fit']}) via {', '.join(o['modules'])}" for o in out]
    answer = f"Modules {via}: {', '.join(mods)}\nSuggested reviewers:\n" + "\n".join(lines)
    return {"answer": answer, "data": {"modules": mods, "reviewers": out}, "path": p.dump()}


def central_modules(g):
    p = Path()
    res = rows(g, """CALL algo.pageRank('Module', 'CO_CHANGES') YIELD node, score
                     WHERE node.kind = 'code' RETURN node.path, score ORDER BY score DESC LIMIT 8""")
    top = [m for m, _ in res]
    for path in top:
        p.node("Module", path)
    for a, b in rows(g, """MATCH (a:Module)-[c:CO_CHANGES]->(b:Module) WHERE a.path IN $top AND b.path IN $top
                           RETURN a.path, b.path""", top=top):
        p.edge(f"Module:{a}", f"Module:{b}", "CO_CHANGES")
    answer = "Most central code (PageRank over co-change):\n" + "\n".join(f"{m} ({s:.3f})" for m, s in res)
    return {"answer": answer, "data": [{"module": m, "pagerank": s} for m, s in res], "path": p.dump()}

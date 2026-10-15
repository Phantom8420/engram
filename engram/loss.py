"""Loss meter: how much important, unwritten knowledge leaves with a set of people."""
from engram.db import rows

HOLDER = 0.3        # KNOWS score that counts as "can still answer questions about it"
DOC_CREDIT = 0.7    # written knowledge only partly replaces a person


def meter(holdings, removed, documented, importance):
    """holdings: {module: {person: score}}. Pure function so it can be tested without a graph."""
    items, total = [], 0.0
    for m, hs in holdings.items():
        w = sum(hs.values())
        imp = importance.get(m, 0.0)
        if not w or not imp:
            continue
        total += imp
        lost = sum(s for p, s in hs.items() if p in removed) / w
        at_risk = lost * (1 - DOC_CREDIT * bool(documented.get(m)))
        left = [p for p, s in hs.items() if p not in removed and s >= HOLDER]
        if lost > 0:
            items.append({"module": m, "importance": imp, "lost": round(lost, 3), "at_risk": round(at_risk, 3),
                          "documented": bool(documented.get(m)), "holders_left": left,
                          "fragile": len(left) <= 1, "weight": imp * at_risk})
    items.sort(key=lambda i: -i["weight"])
    value = sum(i["weight"] for i in items) / total if total else 0.0
    return {"meter": round(100 * value, 1), "items": items, "total": total}


def state(g, scenario_id=None):
    """Read holdings, importance and documentation (including this scenario's interview captures) from the graph."""
    holdings, importance = {}, {}
    for m, imp, p, s in rows(g, """MATCH (p:Person)-[k:KNOWS]->(m:Module {kind:'code'})
                                   RETURN m.path, m.importance, p.handle, k.score"""):
        holdings.setdefault(m, {})[p] = s
        importance[m] = imp or 0.0
    documented = {m: True for (m,) in rows(g, "MATCH (:Doc)-[:DESCRIBES]->(m:Module) RETURN DISTINCT m.path")}
    removed = set()
    if scenario_id:
        removed = {p for (p,) in rows(g, "MATCH (:Scenario {id:$s})-[:REMOVES]->(p:Person) RETURN p.handle", s=scenario_id)}
        documented |= {m: True for (m,) in rows(g, """MATCH (:Scenario {id:$s})-[:HAS]->(:Capture)-[:DOCUMENTS]->(m:Module)
                                                     RETURN DISTINCT m.path""", s=scenario_id)}
    return holdings, removed, documented, importance


def scenario_meter(g, scenario_id):
    return meter(*state(g, scenario_id))


if __name__ == "__main__":
    import sys
    from engram.db import graph
    h, _, d, imp = state(graph())
    r = meter(h, set(sys.argv[1:]), d, imp)
    print(f"knowledge at risk: {r['meter']}%")
    for i in r["items"][:10]:
        print(f"  {i['module']:40} lost {i['lost']:.0%}  {'FRAGILE ' if i['fragile'] else ''}left: {', '.join(i['holders_left'][:3]) or '-'}")

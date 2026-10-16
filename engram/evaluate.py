"""Measurements behind the README numbers.

KNOWS backtest: fork the live graph, cut it back to a past date (later PRs are deleted in the fork, KNOWS is
re-derived as of then), and for every PR in the following year ask whether the top 3 people KNOWS names for the code
it touches include its author, or one of its reviewers. Baselines name the three people with the most KNOWS on
code, and on anything (CI, docs and tests too).
Issue routing is not scored on its own: only 15 issues fixed in the last year were fixed by an existing contributor.
"""
import json
import sys
from pathlib import Path

from engram import REPO, knows, results_file
from engram.db import graph, rows
from engram.fetch import load_prs
from engram.future import fork
from engram.loss import HOLDER
from engram.load import human, kind_of, module_of, ts

YEAR = 365 * 86400


def snapshot(g, cut):
    f = fork(g)
    try:
        f.query("MATCH (x:PR) WHERE x.created_at > $c DETACH DELETE x", {"c": cut})
        knows.derive(f, cut)
        k = {}
        for h, m, s in rows(f, "MATCH (p:Person)-[k:KNOWS]->(m:Module {kind:'code'}) RETURN p.handle, m.path, k.score"):
            k.setdefault(m, {})[h] = s
        active = [h for (h,) in rows(f, """MATCH (p:Person)-[k:KNOWS]->() WITH p, sum(k.score) AS s
                                          RETURN p.handle ORDER BY s DESC LIMIT 3""")]
    finally:
        f.delete()
    return k, active


def top3(k, mods, prior=None):
    """Each module's KNOWS scaled to its best holder, averaged over the modules a PR touches. With a prior (overall
    activity scaled to 0..1, weight 1 fixed before scoring), people active everywhere get a lift."""
    fit = dict(prior or {})
    for m in mods:
        best = max(k[m].values())
        for h, s in k[m].items():
            fit[h] = fit.get(h, 0) + s / best / len(mods)
    return sorted(fit, key=fit.get, reverse=True)[:3]


def backtest(g, years_back):
    cut = knows.asof_now(g) - years_back * YEAR
    k, active = snapshot(g, cut)
    total = {}
    for d in k.values():
        for h, s in d.items():
            total[h] = total.get(h, 0) + s
    busiest = sorted(total, key=total.get, reverse=True)[:3]
    top = max(total.values())
    prior = {h: v / top for h, v in total.items()}
    out = {"from": cut, "busiest_code": busiest, "busiest_all": active}
    for who in ("author", "reviewer"):
        n = hit = blend = base = base2 = 0
        for pr in load_prs():
            if not cut < ts(pr["createdAt"]) <= cut + YEAR:
                continue
            mods = {m for m in {module_of(x["path"]) for x in pr["files"]["nodes"]} if kind_of(m) == "code" and m in k}
            a = human(pr["author"])
            truth = {a} if who == "author" else {human(r["author"]) for r in pr["reviews"]["nodes"]} - {a}
            truth &= set(total)  # only people who existed at the cut can be predicted
            if not mods or not truth:
                continue
            n, hit = n + 1, hit + bool(truth & set(top3(k, mods)))
            blend += bool(truth & set(top3(k, mods, prior)))
            base, base2 = base + bool(truth & set(busiest)), base2 + bool(truth & set(active))
        out[who] = {"prs": n, "knows_hit@3": round(hit / n, 3), "blend_hit@3": round(blend / n, 3), "busiest_code_hit@3": round(base / n, 3),
                    "busiest_all_hit@3": round(base2 / n, 3)}
    return out


def departed(g, min_prs=15):
    """Everyone who stopped: at least min_prs PRs authored or reviewed, then nothing in the last year of data.
    Picked by rule rather than by hand, so the departures test can't be cherry-picked."""
    return [h for (h,) in rows(g, """MATCH (p:Person)-[:AUTHORED|REVIEWED]->(pr:PR)
                                     WITH p, count(DISTINCT pr) AS n, max(pr.created_at) AS last
                                     WHERE n >= $n AND last < $cut RETURN p.handle ORDER BY n DESC""",
                               n=min_prs, cut=knows.asof_now(g) - YEAR)]


def heir_picks(cands, cut, total):
    """Top two heirs for one module from [handle, KNOWS, last_seen] (strongest first), three ways fixed before scoring:
    KNOWS alone; only people active in the six months before the cut; KNOWS scaled to the module's best holder plus
    overall activity scaled to the busiest person, weight 1."""
    top = max(total.values())
    active = [c for c in cands if c[2] >= cut - YEAR / 2] or cands
    blend = sorted(cands, key=lambda c: -(c[1] / cands[0][1] + total.get(c[0], 0) / top))
    return {v: [c[0] for c in xs][:2] for v, xs in (("knows", cands), ("active", active), ("blend", blend))}


def heirs(g, handle):
    """Freeze the graph on the day @handle last contributed and predict, for each code module they held, who takes it
    over (the strongest remaining KNOWS). Check against who did the most work on it in the following year."""
    cut = rows(g, """MATCH (:Person {handle:$h})-[:AUTHORED|REVIEWED|COMMENTED]->(pr:PR) RETURN max(pr.created_at)""",
               h=handle)[0][0]
    f = fork(g)
    try:
        f.query("MATCH (x:PR) WHERE x.created_at > $c DETACH DELETE x", {"c": cut})
        knows.derive(f, cut)
        held = rows(f, """MATCH (:Person {handle:$h})-[k:KNOWS]->(m:Module {kind:'code'}) WHERE k.score >= $t
                          MATCH (p:Person)-[k2:KNOWS]->(m) WHERE p.handle <> $h
                          WITH m, p, k2 ORDER BY k2.score DESC
                          RETURN m.path, collect([p.handle, k2.score, k2.last_seen])""", h=handle, t=HOLDER)
        total = dict(rows(f, """MATCH (p:Person)-[k:KNOWS]->() WHERE p.handle <> $h
                                RETURN p.handle, sum(k.score) AS s ORDER BY s DESC""", h=handle))
    finally:
        f.delete()
    busiest = max(total, key=total.get)
    held = {v: [(m, heir_picks(c, cut, total)[v]) for m, c in held] for v in ("knows", "active", "blend")}
    work = {}
    for pr in load_prs():
        if not cut < ts(pr["createdAt"]) <= cut + YEAR:
            continue
        people = [(human(pr["author"]), 1.0)] + [(human(r["author"]), 0.5) for r in pr["reviews"]["nodes"]]
        for m in {module_of(x["path"]) for x in pr["files"]["nodes"]}:
            for p, w in people:
                if p and p != handle:
                    work.setdefault(m, {})[p] = work.setdefault(m, {}).get(p, 0) + w
    out = {"handle": handle, "busiest": busiest}
    for v, preds in held.items():
        cases = [(m, pred, max(work[m], key=work[m].get)) for m, pred in preds if work.get(m)]
        pre = "heir" if v == "knows" else f"{v}_heir"
        out.update({"modules": len(cases), f"{pre}_hit@1": sum(c[1][:1] == [c[2]] for c in cases),
                    f"{pre}_hit@2": sum(c[2] in c[1] for c in cases), "busiest_hit": sum(c[2] == busiest for c in cases)})
    return out


def decision_precision():
    if REPO != "psf/black":
        return None  # decisions were hand-labelled for Black only
    labels = json.loads(Path("eval/decision_labels.json").read_text(encoding="utf-8"))["labels"]
    return {"sample": len(labels), "precision": round(sum(labels.values()) / len(labels), 3)}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    g = graph()
    deps = [heirs(g, h) for h in departed(g)]
    res = {"knows_backtest": [backtest(g, y) for y in (4, 3, 2, 1)], "departures": [d for d in deps if d["modules"]],
           "decisions": decision_precision()}
    Path(results_file()).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))

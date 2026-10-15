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

from engram import knows
from engram.db import graph, rows
from engram.fetch import load_prs
from engram.future import fork
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


def top3(k, mods):
    """Each module's KNOWS scaled to its best holder, averaged over the modules a PR touches."""
    fit = {}
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
    out = {"from": cut, "busiest_code": busiest, "busiest_all": active}
    for who in ("author", "reviewer"):
        n = hit = base = base2 = 0
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
            base, base2 = base + bool(truth & set(busiest)), base2 + bool(truth & set(active))
        out[who] = {"prs": n, "knows_hit@3": round(hit / n, 3), "busiest_code_hit@3": round(base / n, 3),
                    "busiest_all_hit@3": round(base2 / n, 3)}
    return out


def decision_precision():
    labels = json.loads(Path("eval/decision_labels.json").read_text(encoding="utf-8"))["labels"]
    return {"sample": len(labels), "precision": round(sum(labels.values()) / len(labels), 3)}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    g = graph()
    res = {"knows_backtest": [backtest(g, 2), backtest(g, 1)], "decisions": decision_precision()}
    Path("eval/results.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))
